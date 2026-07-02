import threading
from typing import Tuple, Optional
 
_counter = 0
_lock = threading.Lock()
 
def get_next_key_pair(n_keys: int, need_brain: bool = True) -> Tuple[Optional[int], int]:
    global _counter
    if n_keys <= 0:
        return (None, 0)
    with _lock:
        # Prevent unbounded counter growth
        _counter = _counter % (n_keys * 100)
        if need_brain:
            if n_keys >= 2:
                brain = _counter % n_keys
                chat = (_counter + 1) % n_keys
                _counter += 2
                return (brain, chat)
            else:
                # Single key — no brain separation possible
                _counter += 1
                return (None, 0)
        else:
            chat = _counter % n_keys
            _counter += 1
            return (None, chat)
 