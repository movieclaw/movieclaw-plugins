"""Offline storage doubles; production always receives official PLUGIN_DATA."""
from copy import deepcopy


class MemoryStore:
    def __init__(self):
        self.values = {}
        self.writes = []
        self.fail_phase = None
        self.lose_phase = None

    async def get(self,key,*,scope='global',default=None):
        return deepcopy(self.values.get((scope,key),default))

    async def set(self,key,value,*,scope='global',secret=False):
        assert secret is True
        self.writes.append((key,deepcopy(value),secret))
        if value['phase']==self.fail_phase:raise OSError('synthetic store failure')
        self.values[scope,key] = deepcopy(value)
        if value['phase']==self.lose_phase:raise OSError('synthetic lost store ACK')
