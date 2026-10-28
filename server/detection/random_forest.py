"""Per-device 42-feature Random Forest fall detector."""
from collections import deque
import numpy as np
from server.detection.fsm import FallState

class RandomForestDetector:
    def __init__(self, model, threshold=0.40):
        self.model = model
        self.threshold = threshold
        self.reset(0)

    def reset(self, now):
        self.state = FallState.NORMAL
        self.suspect_features = None
        self.probability = None
        self.history = deque()
        self.window = deque()
        self.votes = deque()

    def update(self, kp, now):
        if self.state == FallState.SUSPECT:
            return self.state
        if kp is None or not kp.image_width or not kp.image_height:
            self.history.clear(); self.window.clear(); self.votes.clear()
            self.probability = None
            return self.state
        shoulder, hip = kp.shoulder_center, kp.hip_center
        x0, y0, x1, y1 = kp.bbox
        w, h = x1-x0, y1-y0
        angle = float(np.degrees(np.arctan2(abs(shoulder[0]-hip[0]), abs(shoulder[1]-hip[1])+1e-6)))
        static = [angle, float(hip[1]/kp.image_height), float(shoulder[1]/kp.image_height), w/kp.image_width, h/kp.image_height, min(h/(w+1e-6),10.0)]
        self.history.append((now, static))
        while self.history and now-self.history[0][0] > 1.0: self.history.popleft()
        old_t, old = self.history[0]
        dt = now-old_t
        if dt < 0.15:
            if len(self.history) == 1:
                self.window.clear(); self.votes.clear(); self.probability = None
            return self.state
        base = static + [(static[1]-old[1])/dt, (static[0]-old[0])/dt, (static[4]-old[4])/dt]
        self.window.append((now, base))
        while self.window and now-self.window[0][0] > 1.5: self.window.popleft()
        if len(self.window)<3 or now-self.window[0][0]<0.8: return self.state
        arr=np.asarray([v for _,v in self.window], dtype=np.float32)
        stats=np.stack([arr.mean(0),arr.std(0),arr.min(0),arr.max(0)],axis=1).ravel()
        vector=np.concatenate([stats,arr[-1,:6]-arr[0,:6]])
        classes=list(self.model.classes_)
        self.probability=float(self.model.predict_proba([vector])[0][classes.index(1)])
        self.votes.append((now,self.probability > self.threshold))
        while self.votes and now-self.votes[0][0] > 1.2: self.votes.popleft()
        if len(self.votes)>=3 and sum(v for _,v in self.votes)/len(self.votes)>=0.5:
            self.state=FallState.SUSPECT
            self.suspect_features={'fall_probability':round(self.probability,4)}
        return self.state
