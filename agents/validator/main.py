from agents.validator.card import DESCRIPTIONS
from common.runtime import FreightAgent
from common.server import serve


class ValidatorAgent(FreightAgent):
    name = "validator"
    # TODO: implement handle() (see PRD section 8)


if __name__ == "__main__":
    serve(ValidatorAgent(), DESCRIPTIONS)
