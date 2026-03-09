"""Thread-safe output buffer for picamera2 H.264 encoder."""
import threading

try:
    from picamera2.outputs import Output as _PicamOutput
except ImportError:
    _PicamOutput = object


class StreamOutput(_PicamOutput):
    """Captures H.264 encoded frames for WebSocket streaming.
    
    Registered as the output for picamera2's H264Encoder.
    outputframe() is called from the encoder thread; get_chunks()
    is called from the async streaming loop.
    """

    def __init__(self):
        if _PicamOutput is not object:
            super().__init__()
        self._chunks = []
        self._lock = threading.Lock()

    def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=None):
        with self._lock:
            self._chunks.append({
                "data": bytes(frame),
                "keyframe": keyframe,
                "timestamp": timestamp,
            })

    def get_chunks(self):
        """Return and clear all pending encoded chunks. Thread-safe."""
        with self._lock:
            chunks = self._chunks
            self._chunks = []
            return chunks

    def start(self):
        pass

    def stop(self):
        with self._lock:
            self._chunks = []
