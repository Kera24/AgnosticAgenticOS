"""In-process event bus for the dashboard's SSE stream.

Thread-safe: orchestration operations run in worker threads and publish;
each connected SSE client drains its own queue. A bounded ring buffer of
recent events supports Last-Event-ID replay after a browser reconnect.
"""
import datetime as _dt
import json
import queue
import threading

RING_SIZE = 400
QUEUE_SIZE = 1000


class EventBus:
    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers = []
        self._ring = []
        self._next_id = 1

    def publish(self, event_type, data=None):
        with self._lock:
            event = {"id": self._next_id, "type": event_type,
                     "ts": _dt.datetime.now().isoformat(timespec="seconds"),
                     "data": data or {}}
            self._next_id += 1
            self._ring.append(event)
            if len(self._ring) > RING_SIZE:
                self._ring = self._ring[-RING_SIZE:]
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass   # slow client: it will resync via Last-Event-ID
        return event

    def subscribe(self, last_event_id=None):
        q = queue.Queue(maxsize=QUEUE_SIZE)
        with self._lock:
            replay = []
            if last_event_id:
                try:
                    last = int(last_event_id)
                    replay = [e for e in self._ring if e["id"] > last]
                except (TypeError, ValueError):
                    pass
            self._subscribers.append(q)
        for event in replay:
            try:
                q.put_nowait(event)
            except queue.Full:
                break
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)


def sse_format(event):
    return "id: %d\nevent: %s\ndata: %s\n\n" % (
        event["id"], event["type"],
        json.dumps({"ts": event["ts"], **event["data"]}, default=str))
