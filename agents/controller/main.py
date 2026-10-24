from agents.controller.card import DESCRIPTIONS
from common.runtime import FreightAgent
from common.server import serve


class ControllerAgent(FreightAgent):
    name = "controller"
    # TODO: implement handle() (see PRD section 8)


if __name__ == "__main__":
    serve(ControllerAgent(), DESCRIPTIONS)
