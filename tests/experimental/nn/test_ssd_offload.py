# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.

"""
Testing SsdFlatParameter and SsdTensorHandle modules.
"""

import tempfile

import numpy as np
import pytest
import torch

import fairscale.experimental.nn.ssd_offload as so
from fairscale.utils import torch_version

# Note: We need the nightly version for SSD offload to work. Hence I am checking for the next PyTorch release.
pytestmark = pytest.mark.skipif(torch_version() < (1, 11, 0), reason="requires torch version >= 1.11.0")


def _init():
    torch.manual_seed(0)
    np.random.seed(0)


def test_write_read():
    _init()

    with tempfile.NamedTemporaryFile() as f:
        ref_tensor = torch.rand((128), dtype=torch.float32)
        test_tensor = torch.zeros_like(ref_tensor)
        assert not torch.equal(ref_tensor, test_tensor)
        so.write(ref_tensor, f.name)
        so.read(test_tensor, f.name)
        assert torch.equal(ref_tensor, test_tensor)


def test_ssd_handle_dispatch_fwd():
    _init()

    with tempfile.NamedTemporaryFile() as f:
        orig_tensor = torch.randn((128))
        ssd_handle = so.SsdTensorHandle.from_tensor(orig_tensor)
        ssd_handle.set_file_params(f.name, 0)
        ssd_handle.to_file(release_tensor_after_write=True)

        assert torch.equal(ssd_handle.to_tensor(), orig_tensor)

        # This should trigger the torch_dispatch code and write
        # back the results to the file
        ssd_handle.add_(1)
        plus1_tensor = orig_tensor.add(1)
        assert torch.equal(ssd_handle.to_tensor(), plus1_tensor)


def test_ssd_handle_dispatch_bwd():
    _init()

    with tempfile.NamedTemporaryFile() as f:
        orig_tensor = torch.randn((4, 4), requires_grad=True)
        orig_copy = orig_tensor.clone().detach().requires_grad_(True)
        ssd_handle = so.SsdTensorHandle.from_tensor(orig_tensor)
        ssd_handle.set_file_params(f.name, 0)
        ssd_handle.to_file(release_tensor_after_write=True)

        assert torch.equal(ssd_handle.to_tensor(), orig_tensor)

        y1 = ssd_handle + 1
        y2 = orig_copy + 1
        y1.sum().backward()
        y2.sum().backward()

        assert torch.equal(ssd_handle.grad, orig_copy.grad)


def test_ssd_handle_train_simple():
    _init()

    with tempfile.NamedTemporaryFile() as f:
        orig_tensor = torch.randn((4, 4), requires_grad=True)

        with torch.no_grad():
            orig_copy = torch.empty_like(orig_tensor)
            orig_copy.copy_(orig_tensor)
            orig_copy.requires_grad = True

        ssd_handle = so.SsdTensorHandle.from_tensor(orig_tensor)
        ssd_handle.set_file_params(f.name, 0)
        ssd_handle.to_file(release_tensor_after_write=True)

        assert torch.equal(ssd_handle.to_tensor(), orig_tensor)
        optimizer_ssd = torch.optim.SGD([ssd_handle], lr=0.1)
        optimizer_orig = torch.optim.SGD([orig_copy], lr=0.1)

        y1 = ssd_handle + 1
        optimizer_ssd.zero_grad()
        y1.sum().backward()
        optimizer_ssd.step()

        y2 = orig_copy + 1
        optimizer_orig.zero_grad()
        y2.sum().backward()
        optimizer_orig.step()

        # make sure we are using the file version not the cached tensor
        ssd_handle.point_to_file(f.name, 0)
        assert torch.equal(ssd_handle.to_tensor(), orig_copy)


def test_ssd_flat_param_train_simple():
    _init()
    with tempfile.NamedTemporaryFile() as f:
        orig_tensor = torch.randn((4, 4))

        with torch.no_grad():
            orig_copy = torch.empty_like(orig_tensor)
            orig_copy.copy_(orig_tensor)
        param = torch.nn.Parameter(orig_copy)

        ssd_flat_param = so.SsdFlatParameter([param], f.name, True)

        assert torch.equal(list(ssd_flat_param.get_param_views())[0], orig_tensor)
        optimizer_ssd = torch.optim.SGD([ssd_flat_param], lr=0.1)
        optimizer_orig = torch.optim.SGD([param], lr=0.1)

        y1 = ssd_flat_param + 1
        optimizer_ssd.zero_grad()
        y1.sum().backward()
        optimizer_ssd.step()

        y2 = param + 1
        optimizer_orig.zero_grad()
        y2.sum().backward()
        optimizer_orig.step()

        # make sure we are using the file version not the cached tensor
        ssd_flat_param.point_to_file(f.name, 0)
        assert torch.equal(list(ssd_flat_param.get_param_views())[0], param)


def test_ssd_flat_parameter_basic():
    _init()
    with tempfile.NamedTemporaryFile() as f:
        refa_param = torch.nn.Parameter(torch.rand((32, 4), dtype=torch.float32))
        refb_param = torch.nn.Parameter(torch.rand((32, 4), dtype=torch.float32))
        refc_param = torch.nn.Parameter(torch.rand((128), dtype=torch.float32))
        ssd_flat_param = so.SsdFlatParameter([refa_param, refb_param, refc_param], f.name, False)

        param_views = list(ssd_flat_param.get_param_views())

        assert refa_param.shape == param_views[0].shape
        assert refb_param.shape == param_views[1].shape
        assert refc_param.shape == param_views[2].shape

        assert torch.equal(refa_param, param_views[0])
        assert torch.equal(refb_param, param_views[1])
        assert torch.equal(refc_param, param_views[2])
        ssd_flat_param.to_file()
