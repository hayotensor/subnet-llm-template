from dataclasses import asdict
from enum import Enum
import threading
import time
from typing import Optional, Tuple

from hivemind.utils.auth import AuthorizerBase

from subnet.health.state_updater import ScoringProtocol
from subnet.substrate.chain_data import RewardsData
from subnet.substrate.chain_functions import activate_subnet, attest, get_block_number, get_epoch_length, get_subnet_data, get_subnet_id_by_path, get_rewards_submission, get_rewards_validator, validate
from subnet.substrate.config import BLOCK_SECS, SubstrateConfigCustom
from subnet.substrate.utils import get_consensus_data, get_next_epoch_start_block, get_submittable_nodes
from hivemind.utils import get_logger

from subnet.utils.math import saturating_div, saturating_sub

logger = get_logger(__name__)

MAX_ATTEST_CHECKS = 3

class AttestReason(Enum):
  WAITING = 1
  ATTESTED = 2
  ATTEST_FAILED = 3
  SHOULD_NOT_ATTEST = 4

class Consensus(threading.Thread):
  """
  Houses logic for validating and attesting consensus data per epochs for rewards

  This can be ran before or during a model activation.

  If before, it will wait until the subnet is successfully voted in, if the proposal to initialize the subnet fails,
  it will not stop running.

  If after, it will begin to validate and or attest epochs
  """
  def __init__(self, path: str, authorizer: AuthorizerBase, substrate: SubstrateConfigCustom):
    super().__init__()
    assert path is not None, "path must be specified"
    assert substrate is not None, "account_id must be specified"
    self.subnet_id = None # Not required in case of not initialized yet
    self.path = path
    self.subnet_accepting_consensus = False
    self.subnet_node_eligible = False
    self.subnet_activated = 9223372036854775807 # max int
    self.last_validated_or_attested_epoch = 0
    self.authorizer = authorizer

    self.substrate_config = substrate
    self.account_id = substrate.account_id

    self.previous_epoch_data = None

    # blockchain constants
    self.epoch_length = int(str(get_epoch_length(self.substrate_config.interface)))

    # initialize DHT client for scoring protocol
    self.scoring_protocol = ScoringProtocol(self.authorizer)

    self.stop = threading.Event()

    self.start()

  def run(self):
    """
    Iterates each epoch, runs the incentives mechanism for the SCP
    """
    while not self.stop.is_set():
      try:
        # get epoch
        block_number = get_block_number(self.substrate_config.interface)

        logger.info("Block height: %s " % block_number)

        epoch = int(block_number / self.epoch_length)
        logger.info("Epoch: %s " % epoch)

        next_epoch_start_block = get_next_epoch_start_block(
          self.epoch_length, 
          block_number
        )
        remaining_blocks_until_next_epoch = next_epoch_start_block - block_number
        
        # skip if already validated or attested epoch
        if epoch <= self.last_validated_or_attested_epoch and self.subnet_accepting_consensus:
          logger.info("Already completed epoch: %s, waiting for the next " % epoch)
          time.sleep(remaining_blocks_until_next_epoch * BLOCK_SECS)
          continue

        # Ensure subnet is activated
        if self.subnet_accepting_consensus == False:
          logger.info("Waiting for subnet activation")
          activated = self._activate_subnet()

          # if given shutdown flag
          # ``_activate_subnet(self)`` can shutdown if the subnet is Null
          if self.stop.is_set():
            logger.info("Consensus thread shutdown, stopping consensus")
            break

          if activated == True:
            continue
          else:
            # Sleep until voting is complete
            time.sleep(BLOCK_SECS)
            continue

        """
        Is subnet node initialized and eligible to submit consensus
        """
        # subnet is eligible to accept consensus
        # check if we are submittable
        # in order to be submittable:
        # - Must stake onchain
        # - Must be Submittable subnet node class
        if self.subnet_node_eligible == False:
          submittable_nodes = get_submittable_nodes(
            self.substrate_config.interface,
            self.subnet_id,
          )

          #  wait until we are submittable
          for node_set in submittable_nodes:
            if node_set.account_id == self.account_id:
              self.subnet_node_eligible = True
              break
          
          if self.subnet_node_eligible == False:
            logger.info("Node not eligible for consensus, sleeping until next epoch")
            time.sleep(remaining_blocks_until_next_epoch * BLOCK_SECS)
            continue

        # is epoch submitted yet

        # is validator?
        validator = self._get_validator(epoch)

        # a validator is not chosen if there are not enough nodes, or the subnet is deactivated
        if validator == None:
          logger.info("Validator not chosen for epoch %s yet, checking next block" % epoch)
          time.sleep(BLOCK_SECS)
          continue
        else:
          logger.info("Validator for epoch %s is %s" % (epoch, validator))

        is_validator = validator == self.account_id
        if is_validator:
          logger.info("We're the chosen validator for epoch %s, validating and auto-attesting..." % epoch)
          # check if validated 
          validated = self._get_validator_consensus_submission(epoch)
          if validated == None:
            success = self.validate()
            # update last validated epoch and continue (this validates and attests in one call)
            if success:
              self.last_validated_or_attested_epoch = epoch
            else:
              logger.warning("Consensus submission unsuccessful, waiting until next block to try again")
              time.sleep(BLOCK_SECS)
              continue
          else:
            # if for any reason on the last attempt it succeeded but didn't propogate
            # because this section should only be called once per epoch and if validator until successful submission of data
            self.last_validated_or_attested_epoch = epoch

          # continue to next epoch, no need to attest
          time.sleep(remaining_blocks_until_next_epoch * BLOCK_SECS)
          continue

        # we are not validator, we must attest or not attest
        # wait until validated by epochs chosen validator

        # get epoch before waiting for validator to validate to ensure we don't get stuck 
        initial_epoch = epoch
        attest_checks = 0
        logger.info("Starting attestation check")
        while True:
          # wait for validator on every block
          time.sleep(BLOCK_SECS)
          block_number = get_block_number(self.substrate_config.interface)
          logger.info("Block height: %s " % block_number)

          epoch = int(block_number / self.epoch_length)
          logger.info("Epoch: %s " % epoch)

          next_epoch_start_block = get_next_epoch_start_block(
            self.epoch_length, 
            block_number
          )
          remaining_blocks_until_next_epoch = next_epoch_start_block - block_number

          # If we made it to the next epoch, break
          # This likely means the chosen validator never submitted consensus data
          if epoch > initial_epoch:
            logger.info("Validator didn't submit epoch %s consensus data, moving to the next epoch" % epoch)
            break

          if attest_checks > MAX_ATTEST_CHECKS:
            logger.info("Failed to attest after %s checks, moving to the next epoch" % attest_checks)
            break

          attest_result, reason = self.attest(epoch)
          if attest_result == False:
            attest_checks += 1
            if reason == AttestReason.WAITING or reason == AttestReason.ATTEST_FAILED:
              continue
            elif reason == AttestReason.ATTESTED:
              # redundant update on `last_validated_or_attested_epoch`
              self.last_validated_or_attested_epoch = epoch
              break
            elif reason == AttestReason.SHOULD_NOT_ATTEST:
              # sleep until end of epoch to check if we should attest

              # sleep until latter half of the epoch to attest
              delta = remaining_blocks_until_next_epoch / 2

              # ensure attestor has at least 2 blocks to run compute
              if delta / 2 < BLOCK_SECS * 2:
                delta = 0

              time.sleep(saturating_sub(delta * BLOCK_SECS, BLOCK_SECS))
              continue
            # If False, still waiting for validator to submit data
            continue
          else:
            # successful attestation, break and go to next epoch
            self.last_validated_or_attested_epoch = epoch
            break
      except Exception as e:
        logger.error("Consensus Error: %s" % e, exc_info=True)

  def validate(self) -> bool:
    """
    Calculate incentives data based on the scoring protocol and submit consensus

    Returns:
      bool: If successful
    """
    # TODO: Add exception handling
    consensus_data = self._get_consensus_data()
    return self._do_validate(consensus_data["peers"])

  def attest(self, epoch: int) -> Tuple[bool, AttestReason]:
    """
    1. Fetches validator incentives data from the blockchain
    2. Calculates incentives data based on the scoring protocol
    3. Compares data to see if should attest
    4. Attests if should attest

    Returns:
      [bool, AttestReason]: If successful, AttestReason for why successful or unsuccessful
    """
    validator_consensus_submission = self._get_validator_consensus_submission(epoch)

    if validator_consensus_submission == None:
      logger.info("Waiting for validator to submit")
      return False, AttestReason.WAITING

    # backup check if validator node restarts in the middle of an epoch to ensure they don't tx again
    if self._has_attested(validator_consensus_submission["attests"]):
      logger.info("Has attested already")
      return False, AttestReason.ATTESTED
    
    validator_consensus_data = RewardsData.list_from_scale_info(validator_consensus_submission["data"])
    
    logger.info("Checking if we should attest the validators submission")
    logger.info("Generating consensus data")
    consensus_data = self._get_consensus_data() # should always return `peers` key
    should_attest = self.should_attest(validator_consensus_data, consensus_data["peers"], epoch)
    logger.info("Should attest is: %s", should_attest)

    if should_attest:
      logger.info("Validators data is confirmed valid, attesting data...")
      attest_is_success = self._do_attest()
      if attest_is_success:
        return True, AttestReason.ATTESTED
      else:
        return False, AttestReason.ATTEST_FAILED
    else:
      logger.info("Validators data is not valid, skipping attestation.")
      return False, AttestReason.SHOULD_NOT_ATTEST
  
  def get_rps(self):
    ...
    
  def _do_validate(self, data) -> bool:
    try:
      receipt = validate(
        self.substrate_config.interface,
        self.substrate_config.keypair,
        self.subnet_id,
        data
      )
      return receipt.is_success
    except Exception as e:
      logger.error("Validation Error: %s" % e)
      return False

  def _do_attest(self) -> bool:
    try:
      receipt = attest(
        self.substrate_config.interface,
        self.substrate_config.keypair,
        self.subnet_id,
      )
      return receipt.is_success
    except Exception as e:
      logger.error("Attestation Error: %s" % e)
      return False
    
  def _get_consensus_data(self):
    """"""
    # TODO: Add exception handling
    consensus_data = get_consensus_data(
      self.substrate_config.interface, 
      self.subnet_id, 
      self.scoring_protocol
    )
    return consensus_data

  def _get_validator_consensus_submission(self, epoch: int):
    """Get and return the consensus data from the current validator"""
    rewards_submission = get_rewards_submission(
      self.substrate_config.interface,
      self.subnet_id,
      epoch
    )
    return rewards_submission

  def _has_attested(self, attestations) -> bool:
    """Get and return the consensus data from the current validator"""
    for data in attestations:
      if data[0] == self.account_id:
        return True
    return False

  def _get_validator(self, epoch):
    validator = get_rewards_validator(
      self.substrate_config.interface,
      self.subnet_id,
      epoch
    )
    return validator
  
  def _activate_subnet(self):
    """
    Activates subnet

    - If in registration period will wait for the subnet to be able to be activated
    - Subnet nodes will wait their turn to activate based on index of entry

    Returns:
      bool: If activated
    """
    subnet_id = get_subnet_id_by_path(self.substrate_config.interface, self.path)
    if subnet_id.meta_info['result_found'] is False:
      logger.error("Cannot find subnet ID at path: %s, shutting down", self.path)
      self.shutdown()
      return False
    
    subnet_data = get_subnet_data(
      self.substrate_config.interface,
      int(str(subnet_id))
    )
    if subnet_data.meta_info['result_found'] is False:
      logger.error("Cannot find subnet data at ID: %s, shutting down", subnet_id)
      self.shutdown()
      return False

    initialized = int(str(subnet_data['initialized']))
    registration_blocks = int(str(subnet_data['registration_blocks']))
    activation_block = initialized + registration_blocks

    # if we didn't activate the subnet, someone indexed before us should have - see logic below
    if subnet_data['activated'] > 0:
      self.subnet_accepting_consensus = True
      self.subnet_id = int(str(subnet_id))
      self.subnet_activated = int(str(subnet_data["activated"]))
      logger.info("Subnet activated")
      return True

    # the following logic is for registering subnets with nodes waiting to activate the subnet onchain

    # randomize activating subnet by node entry index
    # when subnet is in registration, all new subnet nodes are ``Submittable`` classification
    # so we check all submittable nodes
    submittable_nodes = get_submittable_nodes(
      self.substrate_config.interface,
      int(str(subnet_id)),
    )

    submittable = False
    n = 0
    for node_set in submittable_nodes:
      n+=1
      if node_set.account_id == self.account_id:
        submittable = True
        break
    
    # redundant
    # if we made it this far and the node is not yet activated, the subnet should be activated
    if not submittable:
      time.sleep(BLOCK_SECS)
      self._activate_subnet()
    
    min_node_activation_block = activation_block + BLOCK_SECS*10 * (n-1)
    max_node_activation_block = activation_block + BLOCK_SECS*10 * n

    block_number = get_block_number(self.substrate_config.interface)

    # If outside of activation period on both ways
    if block_number < min_node_activation_block:
      delta = min_node_activation_block - block_number
      time.sleep(BLOCK_SECS*delta)
      self._activate_subnet()
    
    # someone of me should have activated by now, keep iterating
    # this will print a warning to manually activate
    if block_number >= max_node_activation_block:
      logger.warning("We skipped subnet activation, attempt to manually activate")
      time.sleep(BLOCK_SECS)
      self._activate_subnet()


    # if within our designated activation block, then activate
    # activation is a no-weight transaction, meaning it costs nothing to do
    if block_number >= min_node_activation_block and block_number < max_node_activation_block:
      # check if activated already by another node
      subnet_data = get_subnet_data(
        self.substrate_config.interface,
        int(str(subnet_id))
      )

      # check if already activated
      if subnet_data['activated'] > 0:
        self.subnet_accepting_consensus = True
        self.subnet_id = int(str(subnet_id))
        self.subnet_activated = True
        logger.info("Subnet activated")
        return True

      # Attempt to activate subnet
      # at this point we assume the subnet is not activated yet
      logger.info("Attempting to activate subnet")
      receipt = activate_subnet(
        self.substrate_config.interface,
        self.substrate_config.keypair,
        int(str(subnet_id)),
      )

      if receipt == None:
        logger.warning("`activate_subnet` Extrinsic failed: Subnet activation failed, check if activated")
        return False

      if receipt.is_success != True:
        logger.warning("`activate_subnet` Extrinsic failed: Subnet activation failed, check if activated")
        return False

      is_success = False
      for event in receipt.triggered_events:
        event_id = event.value['event']['event_id']
        if event_id == 'SubnetActivated':
          logger.info("Subnet activation successful")
          is_success = True
          break
        
      if is_success:
        self.subnet_accepting_consensus = True
        self.subnet_id = int(str(subnet_id))
        self.subnet_activated = True
        return True
      else:
        logger.warning("Subnet activation failed, subnet didn't meet requirements")

    # check if subnet failed to be activated
    # this means:
    # someone else activated it and code miscalculated (contact devs with error if so)
    # or the subnet didn't meet its activation requirements and should revert on the next ``_activate_subnet`` call
    return False

  def should_attest(self, validator_data, my_data, epoch):
    """Checks if two arrays of dictionaries match, regardless of order."""

    # if validator submitted no data, and we have also found the subnet is broken
    if len(validator_data) == 0 and len(my_data) == 0:
      return True
    
    # otherwise, check the data matches
    # at this point, the
    
    # use ``asdict`` because data is decoded from blockchain as dataclass
    # we assume the lists are consistent across all elements
    # Convert validator_data to a set
    set1 = set(frozenset(asdict(d).items()) for d in validator_data)

    # Convert my_data to a set
    set2 = set(frozenset(d.items()) for d in my_data)

    success = set1 == set2

    """
    The following accounts for nodes that go down or back up in the after or before validation submissions and attestations
    - If nodes leaves DHT before before validator submit consensus and returns after before attestation
    - If node leaves DHT after validator submits consensus but still available on the blockchain
    We check the previous epochs data to see if the validator did submit before they left
    """
    if not success and self.previous_epoch_data is not None:
      dif = set1.symmetric_difference(set2)
      success = dif.issubset(self.previous_epoch_data)
    elif not success and self.previous_epoch_data is None:
      """
      If this is the nodes first epoch, check last epochs consensus data
      """
      previous_epoch_validator_data = self._get_validator_consensus_submission(epoch-1)
      if previous_epoch_validator_data != None:
        previous_epoch_data_onchain = set(frozenset(asdict(d).items()) for d in previous_epoch_validator_data)
        dif = set1.symmetric_difference(set2)
        success = dif.issubset(previous_epoch_data_onchain)
    else:
      intersection = set1.intersection(set2)
      logger.info("Matching intersection of %s validator data" % (saturating_div(len(intersection), len(set1)) * 100))
      logger.info("Validator matching intersection of %s my data" % (saturating_div(len(intersection), len(set2)) * 100))

    # update previous epoch data
    self.previous_epoch_data = set2

    return success

  def shutdown(self):
    self.stop.set()
