import torch
import torch.nn as nn
import torch.distributed.rpc as rpc
from torch.distributed.rpc import RRef
import threading
import concurrent.futures

class ToDevice(nn.Module):
    def __init__(self, device):
        super().__init__()
        self.device = device

    def forward(self, x):
        return x.to(self.device)


class SingleProcessPipeline(nn.Sequential):
    def __init__(self, shards, devices):
        super().__init__()
        assert len(shards) == len(devices)
        self.devices = devices

        with concurrent.futures.ThreadPoolExecutor() as executor:
            concurrent.futures.wait([executor.submit(lambda s, d: s.to(d), shards[i], devices[i]) for i in range(len(shards))])

        for i, shard in enumerate(shards):
            self.add_module(f'Shard({devices[i]})', shard)
            if i != len(shards)-1:
                self.add_module(f'ToDevice({devices[i+1]})', ToDevice(devices[i+1]))


class SingleProcessPipelineRPC(nn.Module):
    def __init__(self, shards, devices):
        super().__init__()
        assert len(shards) == len(devices)
        self.underlying = nn.Sequential()
        self.devices = devices
        self._lock = threading.Lock()

        with concurrent.futures.ThreadPoolExecutor() as executor:
            concurrent.futures.wait([executor.submit(lambda s, d: s.to(d), shards[i], devices[i]) for i in range(len(shards))])

        for i, shard in enumerate(shards):
            self.underlying.add_module(f'Shard({devices[i]})', shard)
            if i != len(shards)-1:
                self.underlying.add_module(f'ToDevice({devices[i+1]})', ToDevice(devices[i+1]))

    def forward(self, x_rref):
        x = x_rref.to_here().to(self.devices[0])
        with self._lock:
            out = self.underlying(x)
        return out.cpu()

    def parameter_rrefs(self):
        return [RRef(p) for p in self.parameters()]


class RemoteBaseCPURPC(nn.Module):
    def __init__(self, underlying, device):
        super().__init__()
        self.underlying = underlying.to(device)
        self.device = device
        self._lock = threading.Lock()

    def forward(self, x_rref):
        x = x_rref.to_here().to(self.device)
        with self._lock:
            out = self.underlying(x)
        return out.cpu()

    def parameter_rrefs(self):
        return [RRef(p) for p in self.parameters()]


class RemoteBaseCUDARPC(nn.Module):
    def __init__(self, underlying, device):
        super().__init__()
        self.underlying = underlying.to(device)
        self.device = device
        self._lock = threading.Lock()

    def forward(self, x_rref):
        with self._lock:
            return self.underlying(x_rref.to_here())

    def parameter_rrefs(self):
        return [RRef(p) for p in self.parameters()]


class RPCPipeline(nn.Module):
    def __init__(self, workers_to_shards, workers_to_devices, remote_base_class, split_size=1):
        super().__init__()
        self.split_size = split_size
        assert workers_to_shards.keys() == workers_to_devices.keys()
        self.shards = [rpc.remote(worker, remote_base_class, args=(workers_to_shards[worker], workers_to_devices[worker])) for worker in workers_to_shards.keys()]

    def forward(self, xs):
        # Split the input batch xs into micro-batches, and collect async RPC
        # futures into a list
        out_futures = []
        for x in iter(xs.split(self.split_size, dim=0)):
            x_rref = RRef(x)
            for shard in self.shards[:-1]:
                x_rref = shard.remote().forward(x_rref)
            z_fut = self.shards[-1].rpc_async().forward(x_rref)
            out_futures.append(z_fut)

        # collect and cat all output tensors into one tensor.
        return torch.cat(torch.futures.wait_all(out_futures))

    def parameter_rrefs(self):
        remote_params = []
        for shard in self.shards:
            remote_params.extend(shard.remote().parameter_rrefs().to_here())
        return remote_params
