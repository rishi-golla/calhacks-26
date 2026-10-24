from agents.verification.card import DESCRIPTIONS
from common.runtime import FreightAgent
from common.server import serve


class VerificationAgent(FreightAgent):
    name = "verification"
    # TODO: implement handle() (see PRD section 8)


if __name__ == "__main__":
    serve(VerificationAgent(), DESCRIPTIONS)
