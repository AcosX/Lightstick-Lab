"""Bounded UDP receiver with per-packet validation and lifecycle ownership."""
import socket
import threading

class UdpConnector:
    def __init__(self):
        self._socket = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._status = {'active': False, 'received': 0, 'malformed': 0, 'rejected': 0, 'deduplicated': 0, 'error': ''}

    def __deepcopy__(self, memo):
        return type(self)()

    def start(self, context):
        self.stop()
        self.context = context
        config = {**self.default_config, **context.config}
        if not isinstance(config['host'], str) or type(config['port']) is not int or not 0 <= config['port'] <= 65535:
            raise ValueError('Invalid UDP host/port')
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((config['host'], config['port']))
            sock.settimeout(.1)
        except Exception:
            sock.close()
            raise
        self._socket = sock
        self._stop.clear()
        with self._lock:
            self._status = {'active': True, 'address': sock.getsockname(), 'received': 0,
                            'malformed': 0, 'rejected': 0, 'deduplicated': 0, 'error': ''}
        self._thread = threading.Thread(target=self._run, name=self.id, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        if self._socket is not None:
            self._socket.close()
        self._socket = self._thread = None
        with self._lock:
            self._status['active'] = False

    def status(self):
        with self._lock:
            return dict(self._status)

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    packet, peer = self._socket.recvfrom(65535)
                except socket.timeout:
                    continue
                with self._lock:
                    self._status['received'] += 1
                try:
                    updates = self.parse(packet)
                except ValueError as exc:
                    with self._lock:
                        self._status['malformed'] += 1
                        self._status['error'] = str(exc)
                    continue
                if updates:
                    try:
                        accepted = self.context.submit_update(updates)
                    except (ValueError, RuntimeError) as exc:
                        # Valid input can be unsupported, disconnected or suspended.
                        # Drop it without replaying; keep listening for post-recovery
                        # input. Parser/programming failures still reach the outer guard.
                        with self._lock:
                            self._status['rejected'] += 1
                            self._status['error'] = str(exc)
                    else:
                        with self._lock:
                            self._status['error'] = ''
                            if not accepted:
                                self._status['deduplicated'] += 1
        except Exception as exc:
            with self._lock:
                self._status['error'] = str(exc)
        finally:
            with self._lock:
                self._status['active'] = False
            if self._socket is not None:
                self._socket.close()
