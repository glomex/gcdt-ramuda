"""
Lambda example with external dependency
"""

import logging
from impl.sample import sample

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handle(event, context):
    """Lambda handler
    """
    logger.info("%s - %s", event, context)
    # no ["ramuda_action"] == "ping"
    #if "ramuda_action" in event:
    #    if event["ramuda_action"] == "ping":
    #        return "alive"
    #else:
    sample()
    # some changes here so we can update the handler
    return event
