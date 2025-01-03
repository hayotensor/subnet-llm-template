import argparse
import logging

import configargparse
import torch
from hivemind.proto.runtime_pb2 import CompressionType
from hivemind.utils import limits
from hivemind.utils.logging import get_logger
from humanfriendly import parse_size

from petals.constants import DTYPE_MAP, PUBLIC_INITIAL_PEERS
from petals.server.server import Server
from petals.substrate.chain_functions import register_subnet_node
from petals.substrate.config import SubstrateConfig
from petals.utils.convert_block import QuantType
from petals.utils.version import validate_version

logger = get_logger(__name__)

"""
python -m petals.cli.hypertensor.subnet_node.register --subnet_id 1 --peer_id 12D3KooWBF38f6Y9NE4tMUQRfQ7Yt2HS26hnqUTB88isTZF8bwLs --stake_to_be_added 1000.00 
"""

def main():
    # fmt:off
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--subnet_id", type=str, required=True, help="Subnet ID stored on blockchain. ")
    parser.add_argument("--peer_id", type=str, required=True, help="Peer ID generated using `keygen`")
    parser.add_argument("--stake_to_be_added", type=float, required=True, help="Amount of stake to be added")
    parser.add_argument("--a", type=str, required=False, default=None, help="Unique identifier for subnet node, such as a public key")
    parser.add_argument("--b", type=str, required=False, default=None, help="Non-unique value for subnet node")
    parser.add_argument("--c", type=str, required=False, default=None, help="Non-unique value for subnet node")

    args = parser.parse_args()

    subnet_id = args.subnet_id
    peer_id = args.peer_id
    stake_to_be_added = int(args.stake_to_be_added * 1e18)
    a = args.a
    b = args.b
    c = args.c

    try:
        receipt = register_subnet_node(
            SubstrateConfig.interface,
            SubstrateConfig.keypair,
            subnet_id,
            peer_id,
            stake_to_be_added,
            None,
            None,
            None
        )
        logger.info(receipt)
    except Exception as e:
        logger.error("Error: ", e, exc_info=True)


if __name__ == "__main__":
    main()
