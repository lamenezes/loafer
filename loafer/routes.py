import asyncio
import logging
import time

from .exceptions import DeleteMessage
from .message_translators import AbstractMessageTranslator
from .providers import AbstractProvider

logger = logging.getLogger(__name__)


class CircuitBreaker:
    _failure_count = 0
    _last_failure_time = None

    def __init__(self, *, exceptions, failure_threshold=5, reset_timeout=15):
        self.exceptions = tuple(exceptions)
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout

    def __repr__(self):
        return 'CircuitBreaker(state={!r}, _failure_count={!r}, last_failure_delta={!r})'.format(
            self.state, self._failure_count, time.time() - (self._last_failure_time or time.time()),
        )

    @property
    def state(self):
        if self._failure_count < self.failure_threshold:
            return 'closed'

        if self._last_failure_time and (time.time() - self._last_failure_time) > self.reset_timeout:
            self._last_failure_time = time.time()
            return 'half-open'

        return 'open'

    def open(self, message):
        self._failure_count += 1
        self._last_failure_time = time.time()
        self._raw_message = message

    def close(self):
        self._failure_count = 0
        self._last_failure_time = None
        self._breaker_message = None


class StubCircuitBreaker:
    def __init__(self, **kwargs):
        self.exceptions = ()

    @property
    def state(self):
        return 'closed'

    def open(self, message):
        pass

    def close(self):
        pass


class Route:

    def __init__(self, provider, handler, name='default',
                 message_translator=None, error_handler=None, enabled=True,
                 circuit_breaker=None):
        self.name = name
        self.enabled = enabled
        self.circuit_breaker = circuit_breaker or StubCircuitBreaker()

        assert isinstance(provider, AbstractProvider), 'invalid provider instance'
        self.provider = provider

        self.message_translator = message_translator
        if message_translator:
            assert isinstance(message_translator, AbstractMessageTranslator), \
                'invalid message translator instance'

        self._error_handler = error_handler
        if error_handler:
            assert callable(error_handler), 'error_handler must be a callable object'

        if callable(handler):
            self.handler = handler
            self._handler_instance = None
        else:
            self.handler = getattr(handler, 'handle', None)
            self._handler_instance = handler

        assert self.handler, 'handler must be a callable object or implement `handle` method'

    def __str__(self):
        return '<{}(name={} provider={!r} handler={!r})>'.format(
            type(self).__name__, self.name, self.provider, self.handler)

    def apply_message_translator(self, message):
        processed_message = {'content': message,
                             'metadata': {}}
        if not self.message_translator:
            return processed_message

        translated = self.message_translator.translate(processed_message['content'])
        processed_message['metadata'].update(translated.get('metadata', {}))
        processed_message['content'] = translated['content']
        if not processed_message['content']:
            raise ValueError('{} failed to translate message={}'.format(self.message_translator, message))

        return processed_message

    async def deliver(self, raw_message, loop=None):
        if not self.enabled:
            logger.warning('ignoring message={!r} route={} is not enabled'.format(raw_message, self))
            return False

        try:
            result = await self.run_handler(raw_message, loop)
        except (DeleteMessage, asyncio.CancelledError):
            raise
        except self.circuit_breaker.exceptions:
            logger.debug('circuit open, failure_count={!r}'.format(self.circuit._failure_count))
            self.circuit_breaker.open(raw_message)
            if self.circuit_breaker.state == 'closed':
                return await self.deliver(raw_message, loop)
            raise

        logger.debug('circuit closed')
        self.circuit_breaker.close()

        return result

    async def run_handler(self, raw_message, loop=None):
        message = self.apply_message_translator(raw_message)
        logger.info('delivering message route={}, message={!r}'.format(self, message))
        if asyncio.iscoroutinefunction(self.handler):
            logger.debug('handler is coroutine! {!r}'.format(self.handler))
            return await self.handler(message['content'], message['metadata'])

        logger.debug('handler will run in a separate thread: {!r}'.format(self.handler))
        loop = loop or asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.handler, message['content'], message['metadata'])

    async def error_handler(self, exc_info, message, loop=None):
        logger.info('error handler process originated by message={}'.format(message))

        if self._error_handler is not None:
            if asyncio.iscoroutinefunction(self._error_handler):
                return await self._error_handler(exc_info, message)
            else:
                loop = loop or asyncio.get_event_loop()
                return await loop.run_in_executor(None, self._error_handler, exc_info, message)

        return False

    async def fetch_messages(self):
        state = self.circuit_breaker.state
        if self.circuit_breaker:
            if state == 'open':
                return []

            if state == 'half-open':
                logger.debug('circuit half-open')
                return [self.circuit_breaker._raw_message]

        return await self.provider.fetch_messages()

    def stop(self):
        logger.info('stopping route {}'.format(self))
        self.enabled = False
        self.provider.stop()
        # only for class-based handlers
        if hasattr(self._handler_instance, 'stop'):
            self._handler_instance.stop()
