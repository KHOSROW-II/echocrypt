from collections import deque
class JitterBuffer:
    def __init__(self,capacity=32):
        if capacity<2: raise ValueError("capacity must be >=2")
        self.q=deque(maxlen=capacity)
    def push(self,seq,payload): self.q.append((seq,payload))
    def pop(self): return self.q.popleft() if self.q else None
    def __len__(self): return len(self.q)
