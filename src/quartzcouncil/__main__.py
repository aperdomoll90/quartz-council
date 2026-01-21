import sys
from pathlib import Path

# Add src/ to Python path so imports work without packaging
sys.path.insert(0, str(Path(__file__).parent.parent))

import uvicorn

if __name__ == "__main__":
    uvicorn.run("quartzcouncil.github.webhooks.app:app", host="0.0.0.0", port=8000, reload=True)
