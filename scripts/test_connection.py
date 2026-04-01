import asyncio
import sys
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from config import DATABASE_URL

async def main():
    print(f"\n Connexion a : {DATABASE_URL}\n")
    
    # Timeout plus long + retry
    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        connect_args={
            "timeout": 30,
            "command_timeout": 30,
        }
    )
    
    for tentative in range(1, 4):
        print(f"Tentative {tentative}/3...")
        try:
            async with engine.connect() as conn:
                result = await conn.execute(text("SELECT version(), current_database(), current_user"))
                row = result.fetchone()
                print(f"\nConnexion OK !")
                print(f"  PostgreSQL : {row[0]}")
                print(f"  Base       : {row[1]}")
                print(f"  Utilisateur: {row[2]}\n")
                await engine.dispose()
                return
        except Exception as exc:
            print(f"  Echec : {exc}")
            if tentative < 3:
                print(f"  Retry dans 5s...")
                await asyncio.sleep(5)

    print("\nConnexion ECHOUEE apres 3 tentatives.")
    print("Solutions :")
    print("  1. Verifie que Supabase est actif sur supabase.com")
    print("  2. Teste depuis un autre reseau (Wi-Fi maison)")
    print("  3. Verifie DATABASE_URL dans ton .env\n")
    await engine.dispose()
    sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())