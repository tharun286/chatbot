from sqlalchemy import Column, Integer, String, Text, DateTime
from datetime import datetime

from common.database.base import Base   # use whatever Base your project uses


class SupportTicket(Base):
    __tablename__ = "support_tickets"
    
    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(String, unique=True, index=True)
    session_uuid = Column(String, index=True)
    user_email = Column(String)
    user_name = Column(String)
    user_phone = Column(String)
    domain = Column(String)
    brand = Column(String)
    journey = Column(String)
    original_question = Column(String)
    issue_category = Column(String)
    issue_description = Column(String)
    priority = Column(String, default="normal")
    status = Column(String, default="open")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    ipaddress = Column(String)
