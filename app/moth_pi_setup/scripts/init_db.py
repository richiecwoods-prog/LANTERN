from moth_analysis.db import init_db
from moth_analysis.config import DB_PATH

if __name__ == "__main__":
    init_db()
    print(f"Initialised database: {DB_PATH}")
