import asyncio
import os
import sys
from dotenv import load_dotenv
load_dotenv()
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DATABASE_URL = os.getenv("DATABASE_URL")

async def main():
    print(f"\n Connexion a : {DATABASE_URL}\n")
    engine = create_async_engine(DATABASE_URL, echo=False)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version(), current_database(), current_user"))
            row = result.fetchone()
            print(f"Connexion OK !")
            print(f"  PostgreSQL : {row[0]}")
            print(f"  Base       : {row[1]}")
            print(f"  Utilisateur: {row[2]}\n")
    except Exception as exc:
        print(f"Connexion ECHOUEE : {exc}\n")
        sys.exit(1)
    finally:
        await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())