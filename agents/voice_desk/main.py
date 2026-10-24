from agents.voice_desk.card import DESCRIPTIONS
from common.runtime import FreightAgent
from common.server import serve


class VoiceDeskAgent(FreightAgent):
    name = "voice_desk"
    # TODO: implement handle() (see PRD section 8)


if __name__ == "__main__":
    serve(VoiceDeskAgent(), DESCRIPTIONS)
