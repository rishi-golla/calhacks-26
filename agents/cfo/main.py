from agents.cfo.card import DESCRIPTIONS
from common.runtime import FreightAgent
from common.server import serve


class CfoAgent(FreightAgent):
    name = "cfo"
    # TODO: implement handle() (see PRD section 8)


if __name__ == "__main__":
    serve(CfoAgent(), DESCRIPTIONS)
