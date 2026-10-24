from agents.shipper_ar.card import DESCRIPTIONS
from common.runtime import FreightAgent
from common.server import serve


class ShipperArAgent(FreightAgent):
    name = "shipper_ar"
    # TODO: implement handle() (see PRD section 8)


if __name__ == "__main__":
    serve(ShipperArAgent(), DESCRIPTIONS)
