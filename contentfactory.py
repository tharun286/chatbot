from datetime import datetime
from sqlalchemy import Integer, String, DateTime, Text, UniqueConstraint, Boolean, Float, Date, ForeignKey, JSON, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from common.database.base import Base


class UserUsageMetrics(Base):
    __tablename__ = "user_usage_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        unique=True,
        index=True
    )

    allowed_domains: Mapped[str] = mapped_column(
        String(255),
        default="sales,hls",
        nullable=False
    )

    daily_cost_limit: Mapped[float] = mapped_column(
        Float,
        default=1.0,
        nullable=False
    )

    today_cost: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        nullable=False
    )

    last_reset_date: Mapped[Date] = mapped_column(
        Date,
        nullable=True
    )


class SharePointDocument(Base):
    __tablename__ = "sharepoint_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    document_unique_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    version_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    status: Mapped[str | None] = mapped_column(String(255), nullable=True)
    format: Mapped[str | None] = mapped_column(String(255), nullable=True)

    version_modified_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    microsite_status: Mapped[str] = mapped_column(String(255), default="Pending")

    document_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    microsite_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    microsite_creation_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    brand_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    brand_logo: Mapped[str | None] = mapped_column(Text, nullable=True)
    market_name: Mapped[str] = mapped_column(String(255), default="United States")
    asset_type: Mapped[str] = mapped_column(String(255), default="Sales Proposal")
    domain: Mapped[str] = mapped_column(String(255), default="sales")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class HLSDocumentType(Base):
    __tablename__ = "hls_document_types"
    __table_args__ = (
        UniqueConstraint("name", "domain", name="uq_hls_document_types_name_domain"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class HLSMarket(Base):
    __tablename__ = 'hls_markets'
    __table_args__ = (UniqueConstraint('name', 'domain', name='uq_hls_markets_name_domain'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class HLSVeevaDocument(Base):
    __tablename__ = "hls_veeva_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_unique_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    version_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    filename__v: Mapped[str | None] = mapped_column(String(500), nullable=True)
    name__v: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status__v: Mapped[str | None] = mapped_column(String(100), nullable=True)
    brand_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    document_type_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    market_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)

# -------------------------------------------------
# MCS‑only SharePoint asset model (new)
# Mirrors all columns from HLSVeevaDocMicrosites and adds brand_name,
# market_name, and asset_type.
# -------------------------------------------------

class MCSSharepointAsset(Base):
    """Table for SharePoint assets used by the MCS domain."""
    __tablename__ = "sales_sharepoint_assets"
    # Primary key – use a UUID string for consistency with other assets
    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    # Core document fields
    document_unique_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    version_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    filename__v: Mapped[str | None] = mapped_column(String(500), nullable=True)
    name__v: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status__v: Mapped[str | None] = mapped_column(String(100), nullable=True)
    format__v: Mapped[str | None] = mapped_column(String(100), nullable=True)
    version_modified_date__v: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Microsite fields
    microsite_status: Mapped[str | None] = mapped_column(String(100), default="Yet to Start")
    document_path: Mapped[str | None] = mapped_column(String(5000), nullable=True)
    microsite_path: Mapped[str | None] = mapped_column(String(5000), nullable=True)
    microsite_creation_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    index_status: Mapped[str | None] = mapped_column(String(100), default="Yet to Start")
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # UI fields
    market_name: Mapped[str | None] = mapped_column(String(128), default="United States")
    asset_type: Mapped[str | None] = mapped_column(String(128), default="Sales Proposal")
    brand_name: Mapped[str | None] = mapped_column(String(256), default="ZenseAI Sales Content Factory")
    brand_logo: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=func.now(), onupdate=func.now())




class PresentationStyleTemplate(Base):
    __tablename__ = "presentation_style_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    brand_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    references: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)






class SalesCFeedback(Base):
    """Stores user feedback (star rating + text) for Sales Content Factory generated content."""
    __tablename__ = 'sales_cf_feedback'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transaction_uuid: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    format_type: Mapped[str | None] = mapped_column(String(100), nullable=True)  # 'movie maker', 'presentation +', 'audio summary', 'video', 'podcast'
    domain: Mapped[str | None] = mapped_column(String(50), nullable=True)
    star_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)       # 1-5 or NULL if skipped
    feedback_text: Mapped[str | None]= mapped_column(Text, nullable=True)        # Text comment or NULL if skipped
    user_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    user_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class HLSWorkflowRun(Base):
    __tablename__ = "hls_workflow_runs"

    workflow_run_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    workflow_type_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    workflow_type: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    user_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    overall_progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class HLSWorkflowStep(Base):
    __tablename__ = "hls_workflow_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_run_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("hls_workflow_runs.workflow_run_id"),
        nullable=False,
        index=True,
    )
    step_id: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    transaction_uuid: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class HLSVeevaDocMicrosites(Base):
    __tablename__ = "hls_veeva_doc_microsites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_unique_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    version_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    filename__v: Mapped[str | None] = mapped_column(String(500), nullable=True)
    name__v: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status__v: Mapped[str | None] = mapped_column(String(100), nullable=True)
    format__v: Mapped[str | None] = mapped_column(String(100), nullable=True)
    version_modified_date__v: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    microsite_status: Mapped[str] = mapped_column(String(100), default="Yet to Start")
    document_path: Mapped[str | None] = mapped_column(String(5000), nullable=True)
    microsite_path: Mapped[str | None] = mapped_column(String(5000), nullable=True)
    microsite_creation_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    index_status: Mapped[str] = mapped_column(String(100), default="Yet to Start")
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)


#Models for Retail content factory

class RetailMarket(Base):
    __tablename__ = "retail_markets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    sku_code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    category: Mapped[str] = mapped_column(String(255), nullable=False)
    brand: Mapped[str] = mapped_column(String(255), nullable=False)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)

    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    full_description: Mapped[str | None] = mapped_column(Text, nullable=True)

    grades: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    features: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)

    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    reviews_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    category_flag: Mapped[str | None] = mapped_column(String(50), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


    pricing: Mapped[list["RetailPricing"]] = relationship(
        "RetailPricing",
        backref="product",
        lazy="select",
        cascade="all, delete-orphan"
    )


class RetailPricing(Base):
    __tablename__ = "retail_pricing"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    sku_code: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("retail_markets.sku_code"),
        nullable=False
    )

    country: Mapped[str] = mapped_column(String(50), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False)

    cost_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    selling_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    promotional_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    promotional_discount: Mapped[float | None] = mapped_column(Float, nullable=True)

    price_label: Mapped[str | None] = mapped_column(String(100), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class HLSConstants(Base):
    __tablename__ = "hls_constants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    field_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    field_value: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow
    )


class RetailBrand(Base):
    __tablename__ = "retail_brands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    brand_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    tone: Mapped[str | None] = mapped_column(String(255), nullable=True)
    category: Mapped[str | None] = mapped_column(String(255), nullable=True)
    focus_intent: Mapped[str | None] = mapped_column(Text, nullable=True)
    guardrails: Mapped[str | None] = mapped_column(Text, nullable=True)
    other_information: Mapped[str | None] = mapped_column(Text, nullable=True)  # LONGTEXT → Text

    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.current_timestamp(),
        onupdate=func.current_timestamp()
    )
class HLSBrand(Base):
    __tablename__ = "hls_brands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    brand_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        index=True
    )  
    fieldname: Mapped[str] = mapped_column(String(100), nullable=False)
    fieldvalue: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow
    )
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)



class HLSTransaction(Base):
    __tablename__ = "hls_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        nullable=False
    )
    format_type: Mapped[str] = mapped_column(String(50), nullable=False)
    input_file_paths: Mapped[str | None] = mapped_column(String(5000), nullable=True)
    output_file_path: Mapped[str | None] = mapped_column(String(5000), nullable=True)
    mlr_file_path: Mapped[str | None] = mapped_column(String(5000), nullable=True)
    edited_file_paths: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_person: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow
    )
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_workflow: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False
    )
    workflow_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    workflow_run_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        index=True
    )
    workflow_step_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, default=0, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, default=0, nullable=True)
    total_cost: Mapped[float | None] = mapped_column(Float, default=0.0, nullable=True)


class HLSHarvestedClaim(Base):
    __tablename__ = "hls_harvested_claims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transaction_uuid: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    documents: Mapped[list | dict | None] = mapped_column(JSON, nullable=True)
    source_documents: Mapped[list | dict | None] = mapped_column(JSON, nullable=True)
    brand_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    uuid: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    claims: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_pushed_to_promomats: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

#new table for analytic agent
class RetailProductAnalytics(Base):
    __tablename__ = "retail_product_analytics"

    sku_id: Mapped[str] = mapped_column(String(100), primary_key=True, nullable=False)
    category: Mapped[str | None] = mapped_column(String(255), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(255), nullable=True)
    flag_prior: Mapped[str | None] = mapped_column(String(100), nullable=True)
    clicks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    product_views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    add_to_cart: Mapped[int | None] = mapped_column(Integer, nullable=True)
    purchases: Mapped[int | None] = mapped_column(Integer, nullable=True)
    units_sold_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revenue: Mapped[float | None] = mapped_column(Float, nullable=True)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class HLSChatbotHistory(Base):
    __tablename__ = "hls_chatbot_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # session_uuid groups all messages from one chat session into a single row
    session_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # chat_history stores the full conversation as a JSON array: [{role, content, timestamp}]
    chat_history: Mapped[str | None] = mapped_column(Text, nullable=True)
    # legacy columns — kept nullable, no longer written by new code
    query: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    user_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    ipaddress: Mapped[str | None] = mapped_column(String(100), nullable=True)
    adverse_event: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, index=True)
    ticket_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    session_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    user_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_phone: Mapped[str | None] = mapped_column(String(100), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(255), nullable=True)
    journey: Mapped[str | None] = mapped_column(String(50), nullable=True)
    original_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    issue_category: Mapped[str | None] = mapped_column(String(255), nullable=True)
    issue_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(String(50), nullable=False, default="normal")
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    ipaddress: Mapped[str | None] = mapped_column(String(100), nullable=True)

