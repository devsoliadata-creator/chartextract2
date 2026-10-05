"""Recover cited files from a completed response; never invoke a model.

Retired: an artifact attempt no longer stages request.json/response.json on disk (see
src/workflow/artifact_agent.py and the panel's log.json), so there is nothing left here to
replay from. If a Code Interpreter call needs recovering, look up its response_id in the
panel's log.json (src/models/layout.log_file) and re-download its cited files directly with
src.workflow.artifact_agent.download_cited_files against a live Foundry response payload.
"""

import sys

MESSAGE = __doc__.strip()

if __name__ == "__main__":
    print(MESSAGE)
    sys.exit(1)
