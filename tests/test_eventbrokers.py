from __future__ import annotations

from concurrent.futures import Future
from datetime import datetime, timezone
from queue import Empty, Queue
from typing import Callable

import pytest
from _pytest.fixtures import FixtureRequest
from _pytest.logging import LogCaptureFixture
from anyio import create_memory_object_stream, fail_after

from apscheduler.abc import AsyncEventBroker, EventBroker, Serializer
from apscheduler.events import Event, ScheduleAdded


@pytest.fixture
def local_broker() -> EventBroker:
    from apscheduler.eventbrokers.local import LocalEventBroker

    return LocalEventBroker()


@pytest.fixture
def local_async_broker() -> AsyncEventBroker:
    from apscheduler.eventbrokers.async_local import LocalAsyncEventBroker

    return LocalAsyncEventBroker()


@pytest.fixture
def redis_broker(serializer: Serializer) -> EventBroker:
    from apscheduler.eventbrokers.redis import RedisEventBroker

    broker = RedisEventBroker.from_url('redis://localhost:6379')
    broker.serializer = serializer
    return broker


@pytest.fixture
def mqtt_broker(serializer: Serializer) -> EventBroker:
    from paho.mqtt.client import Client

    from apscheduler.eventbrokers.mqtt import MQTTEventBroker

    return MQTTEventBroker(Client(), serializer=serializer)


@pytest.fixture
async def asyncpg_broker(serializer: Serializer) -> AsyncEventBroker:
    from asyncpg import create_pool

    from apscheduler.eventbrokers.asyncpg import AsyncpgEventBroker

    pool = await create_pool('postgres://postgres:secret@localhost:5432/testdb')
    broker = AsyncpgEventBroker.from_asyncpg_pool(pool)
    broker.serializer = serializer
    yield broker
    await pool.close()


@pytest.fixture(params=[
    pytest.param(pytest.lazy_fixture('local_broker'), id='local'),
    pytest.param(pytest.lazy_fixture('redis_broker'), id='redis',
                 marks=[pytest.mark.external_service]),
    pytest.param(pytest.lazy_fixture('mqtt_broker'), id='mqtt',
                 marks=[pytest.mark.external_service])
])
def broker(request: FixtureRequest) -> Callable[[], EventBroker]:
    return request.param


@pytest.fixture(params=[
    pytest.param(pytest.lazy_fixture('local_async_broker'), id='local'),
    pytest.param(pytest.lazy_fixture('asyncpg_broker'), id='asyncpg',
                 marks=[pytest.mark.external_service])
])
def async_broker(request: FixtureRequest) -> Callable[[], AsyncEventBroker]:
    return request.param


class TestEventBroker:
    def test_publish_subscribe(self, broker: EventBroker) -> None:
        queue = Queue()
        with broker:
            broker.subscribe(queue.put_nowait)
            broker.subscribe(queue.put_nowait)
            event = ScheduleAdded(
                schedule_id='schedule1',
                next_fire_time=datetime(2021, 9, 11, 12, 31, 56, 254867, timezone.utc))
            broker.publish(event)
            event1 = queue.get(timeout=3)
            event2 = queue.get(timeout=1)

        assert event1 == event2
        assert isinstance(event1, ScheduleAdded)
        assert isinstance(event1.timestamp, datetime)
        assert event1.schedule_id == 'schedule1'
        assert event1.next_fire_time == datetime(2021, 9, 11, 12, 31, 56, 254867, timezone.utc)

    def test_subscribe_one_shot(self, broker: EventBroker) -> None:
        queue = Queue()
        with broker:
            broker.subscribe(queue.put_nowait, one_shot=True)
            event = ScheduleAdded(
                schedule_id='schedule1',
                next_fire_time=datetime(2021, 9, 11, 12, 31, 56, 254867, timezone.utc))
            broker.publish(event)
            event = ScheduleAdded(
                schedule_id='schedule2',
                next_fire_time=datetime(2021, 9, 12, 8, 42, 11, 968481, timezone.utc))
            broker.publish(event)
            received_event = queue.get(timeout=3)
            with pytest.raises(Empty):
                queue.get(timeout=0.1)

        assert isinstance(received_event, ScheduleAdded)
        assert received_event.schedule_id == 'schedule1'

    def test_unsubscribe(self, broker: EventBroker, caplog) -> None:
        queue = Queue()
        with broker:
            subscription = broker.subscribe(queue.put_nowait)
            broker.publish(Event())
            queue.get(timeout=3)

            subscription.unsubscribe()
            broker.publish(Event())
            with pytest.raises(Empty):
                queue.get(timeout=0.1)

    def test_publish_no_subscribers(self, broker: EventBroker, caplog: LogCaptureFixture) -> None:
        with broker:
            broker.publish(Event())

        assert not caplog.text

    def test_publish_exception(self, broker: EventBroker, caplog: LogCaptureFixture) -> None:
        def bad_subscriber(event: Event) -> None:
            raise Exception('foo')

        timestamp = datetime.now(timezone.utc)
        event_future: Future[Event] = Future()
        with broker:
            broker.subscribe(bad_subscriber)
            broker.subscribe(event_future.set_result)
            broker.publish(Event(timestamp=timestamp))

            event = event_future.result(3)
            assert isinstance(event, Event)
            assert event.timestamp == timestamp
            assert 'Error delivering Event' in caplog.text


@pytest.mark.anyio
class TestAsyncEventBroker:
    async def test_publish_subscribe(self, async_broker: AsyncEventBroker) -> None:
        send, receive = create_memory_object_stream(2)
        async with async_broker:
            async_broker.subscribe(send.send)
            async_broker.subscribe(send.send_nowait)
            event = ScheduleAdded(
                schedule_id='schedule1',
                next_fire_time=datetime(2021, 9, 11, 12, 31, 56, 254867, timezone.utc))
            await async_broker.publish(event)

            with fail_after(3):
                event1 = await receive.receive()
                event2 = await receive.receive()

        assert event1 == event2
        assert isinstance(event1, ScheduleAdded)
        assert isinstance(event1.timestamp, datetime)
        assert event1.schedule_id == 'schedule1'
        assert event1.next_fire_time == datetime(2021, 9, 11, 12, 31, 56, 254867, timezone.utc)

    async def test_subscribe_one_shot(self, async_broker: AsyncEventBroker) -> None:
        send, receive = create_memory_object_stream(2)
        async with async_broker:
            async_broker.subscribe(send.send, one_shot=True)
            event = ScheduleAdded(
                schedule_id='schedule1',
                next_fire_time=datetime(2021, 9, 11, 12, 31, 56, 254867, timezone.utc))
            await async_broker.publish(event)
            event = ScheduleAdded(
                schedule_id='schedule2',
                next_fire_time=datetime(2021, 9, 12, 8, 42, 11, 968481, timezone.utc))
            await async_broker.publish(event)

            with fail_after(3):
                received_event = await receive.receive()

            with pytest.raises(TimeoutError), fail_after(0.1):
                await receive.receive()

        assert isinstance(received_event, ScheduleAdded)
        assert received_event.schedule_id == 'schedule1'

    async def test_unsubscribe(self, async_broker: AsyncEventBroker) -> None:
        send, receive = create_memory_object_stream()
        async with async_broker:
            subscription = async_broker.subscribe(send.send)
            await async_broker.publish(Event())
            with fail_after(3):
                await receive.receive()

            subscription.unsubscribe()
            await async_broker.publish(Event())
            with pytest.raises(TimeoutError), fail_after(0.1):
                await receive.receive()

    async def test_publish_no_subscribers(self, async_broker: AsyncEventBroker,
                                          caplog: LogCaptureFixture) -> None:
        async with async_broker:
            await async_broker.publish(Event())

        assert not caplog.text

    async def test_publish_exception(self,  async_broker: AsyncEventBroker,
                                     caplog: LogCaptureFixture) -> None:
        def bad_subscriber(event: Event) -> None:
            raise Exception('foo')

        timestamp = datetime.now(timezone.utc)
        send, receive = create_memory_object_stream()
        async with async_broker:
            async_broker.subscribe(bad_subscriber)
            async_broker.subscribe(send.send)
            await async_broker.publish(Event(timestamp=timestamp))

            received_event = await receive.receive()
            assert received_event.timestamp == timestamp
            assert 'Error delivering Event' in caplog.text
