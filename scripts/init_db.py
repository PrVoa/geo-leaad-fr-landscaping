import asyncio, os, sys
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()
from sqlalchemy import Column, DateTime, Float, Integer, String, Text, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import DeclarativeBase

DATABASE_URL = os.getenv("DATABASE_URL")

class Base(DeclarativeBase):
    pass

class GridTask(Base):
    __tablename__ = "grid_tasks"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    min_lat       = Column(Float,   nullable=False)
    min_lon       = Column(Float,   nullable=False)
    max_lat       = Column(Float,   nullable=False)
    max_lon       = Column(Float,   nullable=False)
    status        = Column(String(50), default="pending")
    results_count = Column(Integer,    default=0)

class Landscaper(Base):
    __tablename__ = "landscapers"
    place_id     = Column(Text,    primary_key=True)
    name         = Column(Text,    nullable=False)
    phone        = Column(Text,    nullable=True)
    address      = Column(Text,    nullable=True)
    website      = Column(Text,    nullable=True)
    rating       = Column(Float,   nullable=True)
    review_count = Column(Integer, nullable=True)
    latitude     = Column(Float,   nullable=True)
    longitude    = Column(Float,   nullable=True)
    maps_url     = Column(Text,    nullable=True)
    scraped_at   = Column(DateTime, default=datetime.utcnow)

async def main():
    print(f"\n{'='*50}")
    print("  init_db.py - Initialisation de la base")
    print(f"{'='*50}\n")
    engine = create_async_engine(DATABASE_URL, echo=False)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text("SELECT version()"))
            print(f"PostgreSQL connecte : {result.scalar()}\n")
            await conn.run_sync(Base.metadata.create_all)
        print("Tables creees :")
        for t in Base.metadata.tables:
            print(f"  • {t}")
        print("\nBase de donnees prete !\n")
    except Exception as exc:
        print(f"ERREUR : {exc}\n")
        sys.exit(1)
    finally:
        await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())