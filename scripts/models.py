from datetime import datetime
from sqlalchemy import Column, DateTime, Float, Integer, Text
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Landscaper(Base):
    __tablename__ = "landscapers"

    place_id     = Column(Text, primary_key=True)
    name         = Column(Text, nullable=False)
    phone        = Column(Text, nullable=True)
    address      = Column(Text, nullable=True)
    website      = Column(Text, nullable=True)
    email        = Column(Text, nullable=True)
    rating       = Column(Float, nullable=True)
    review_count = Column(Integer, nullable=True)
    maps_url     = Column(Text, nullable=True)
    scraped_at   = Column(DateTime, default=datetime.utcnow)
    type_activite = Column(Text, nullable=True)   # creation | entretien | mixte | inconnu
    score_icp     = Column(Integer, nullable=True) # 0-100 : correspondance ICP
    mots_detectes = Column(Text, nullable=True)   # mots-clés ICP trouvés (comma-separated)


class VilleProgress(Base):
    """Suivi de progression par ville — permet de reprendre après un crash."""
    __tablename__ = "villes_scraping"

    dept     = Column(Text, primary_key=True)   # clé composite (dept, ville)
    ville    = Column(Text, primary_key=True)
    status   = Column(Text, default="pending", nullable=False)  # pending | done
    count    = Column(Integer, default=0)
    done_at  = Column(DateTime, nullable=True)
