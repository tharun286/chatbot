import os
import re
import json
import uuid as _uuid
from datetime import datetime

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from langchain_community.vectorstores import FAISS
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

from ..hls_platform.utils import initialize_llm, initialize_5o_mini_llm
from .embedding_provider_p import get_azure_embeddings
from .greetings import greeting_phrases
from ..utils.utils import getOSpecifiers
from ..configurations.session import get_session, AsyncSessionLocal
from common.database.models.content_factory import (HLSChatbotHistory,SupportTicket)

from ..repository.zenseai_content_factory_repo import get_sources_from_db_repo


def _get_index_folder(domain='hls'):
    sys_type, _ = getOSpecifiers()

    linux_roots = {
        'hls': '/apps/ebuddy/HLS_chatbot_index',
        'zenseai': '/apps/ebuddy/Zenseai_CF_index',
        'sales': '/apps/ebuddy/Sales_chatbot_index',
    }

    win_names = {
        'hls': 'HLS_chatbot_index',
        'zenseai': 'Zenseai_CF_index',
        'sales': 'Sales_chatbot_index',
    }

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    if sys_type == 'linux':
        return linux_roots.get(domain, linux_roots['hls'])

    return os.path.join(base_dir, win_names.get(domain, win_names['hls']))


def _refine_query(query, conversation_history, llm):
    conv_str = str(conversation_history).replace('{', '{{').replace('}', '}}')

    prompt_template = f"""
    You are a concise and intelligent query refinement assistant.
    Your goal:
    - Refine the user's latest message into a clear, complete standalone query.
    - Use the full conversation history to understand the user's intent and context.
    - Correct grammar or phrasing errors while keeping the meaning and tone natural.
    - Avoid unnecessary rewording — only improve clarity and completeness.
    Conversation History:
    {{conv_str}}
    Latest User Query:
    {{query}}
    Output:
    Provide ONLY the refined query as one line of plain text. Do not include explanations, greetings, or formatting.
    """

    chain = ChatPromptTemplate.from_messages([
        ("system", prompt_template),
        ("human", "{input}")
    ]) | llm | StrOutputParser()

    return chain.invoke({
        "input": query,
        "conv_str": conv_str,
        "query": query
    })


def _document_to_dict(doc):
    clean_metadata = {
        k: v
        for k, v in doc.metadata.items()
        if k != 'chunk_id'
    }

    return {
        "page_content": doc.page_content,
        "metadata": clean_metadata
    }


async def _find_match(query, domain='hls', db=None):
    try:
        index_folder = _get_index_folder(domain)
        embeddings = await get_azure_embeddings(db)

        index = FAISS.load_local(
            index_folder,
            embeddings,
            "index",
            allow_dangerous_deserialization=True
        )

        results = index.similarity_search(query, k=3)

        similar_chunk_ids = sorted([
            int(doc.metadata["chunk_id"])
            for doc in results
        ])

        expanded_ids = set()

        for cid in similar_chunk_ids:
            expanded_ids.update([cid - 1, cid, cid + 1])

        expanded_results = []

        for cid in sorted(expanded_ids):
            expanded_results.extend(
                doc
                for doc in index.docstore._dict.values()
                if int(doc.metadata.get("chunk_id", -1)) == cid
            )

        context = [
            _document_to_dict(doc)
            for doc in expanded_results
        ]

        # Collect unique document IDs from results
        doc_ids = list({
            doc.metadata.get("document_unique_id")
            for doc in results
            if doc.metadata.get("document_unique_id")
        })

        return context, doc_ids

    except FileNotFoundError:
        return {
            "error": f"Index not found at '{_get_index_folder(domain)}'."
        }, []

    except Exception as e:
        return {
            "error": f"Error during FAISS search: {str(e)}"
        }, []


# ---------------------------------------------------------------------------
# Brand configuration for Aetheris BioGroup therapeutic brands
# ---------------------------------------------------------------------------

BRAND_CONFIG = {
    'synapta': {
        'display_name': 'Synapta',
        'therapeutic_area': 'Neurology',
        'tone': 'Empathetic, calm, and cerebral',
        'tagline': 'Reconnecting the Self',
        'empathy_style': (
            "calm and deeply compassionate — first acknowledge the patient's "
            "neurological experience with genuine warmth before addressing their question"
        ),
        'patient_persona': 'Maya',
        'hcp_persona': 'Dr. Alex',
    },
    'verveen': {
        'display_name': 'Verveen',
        'therapeutic_area': 'Immunology',
        'tone': 'Encouraging, gentle, and resilient',
        'tagline': 'Defense in Harmony',
        'empathy_style': (
            "gentle and encouraging — honor the patient's daily resilience and "
            "validate the challenges of living with an autoimmune condition before answering"
        ),
        'patient_persona': 'Jordan',
        'hcp_persona': 'Dr. Reyes',
    },
    'kinetix': {
        'display_name': 'Kinetix',
        'therapeutic_area': 'Oncology',
        'tone': 'Bold, clinical, and relentless',
        'tagline': 'Precision. Power. Persistence.',
        'empathy_style': (
            "strong and determined — stand firmly alongside the patient, "
            "acknowledging the weight of their oncology journey before providing information"
        ),
        'patient_persona': 'Marcus',
        'hcp_persona': 'Dr. Morgan',
    },
}

# Primary brand name keywords used for detection (checked against metadata + content)
_BRAND_KEYWORDS = {
    'synapta': ['synapta'],
    'verveen': ['verveen'],
    'kinetix': ['kinetix', 'kinetrix'],
}

# Fallback personas when brand is unknown
PATIENT_SUPPORT_PERSONA = 'James'
MEDICAL_TEAM_PERSONA = 'Dr. Rivera'

# Adverse-event reporting phone number
AE_REPORT_PHONE = '+1-800-332-1088'

async def _get_stored_journey(session_uuid, db):
    """Return the last persisted journey ('hcp' or 'patient') for this session, or None."""
    if not session_uuid or not db:
        return None
    try:
        stmt = select(HLSChatbotHistory).where(
            HLSChatbotHistory.session_uuid == session_uuid
        )
        result = await db.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing:
            history = json.loads(existing.chat_history or '[]')
            for msg in reversed(history):
                if isinstance(msg, dict) and msg.get('role') == 'user':
                    stored = msg.get('journey')
                    if stored in ('hcp', 'patient'):
                        return stored
    except Exception:
        pass
    return None


def _classify_journey_llm(query, conversation_history, llm):
    """Use the LLM to decide whether the current message signals HCP, patient, or no change.

    Returns 'hcp', 'patient', or 'unchanged'.
    The LLM understands natural language so it catches phrasings that keyword
    lists miss: 'as the treating oncologist', 'my patient is having side effects',
    'I was just diagnosed', etc.
    """
    conv_str = str(conversation_history)[:1500]

    system_text = (
        'You are a role classifier for a medical chatbot. '
        'Classify the role of the PERSON SENDING THE LATEST MESSAGE based on what they say about themselves.\n\n'
        'Return ONLY valid JSON: {{"journey": "hcp" | "patient" | "unchanged"}}\n\n'
        'Rules:\n'
        '- "hcp": the user indicates they are a healthcare professional -- doctor, nurse, pharmacist, '
        'specialist, prescriber, MSL, or similar -- OR uses phrases like "my patient", "I am treating", '
        '"from a clinical perspective", "as an oncologist", "I am prescribing".\n'
        '- "patient": the user indicates they are a patient or caregiver -- they describe taking the '
        'medication themselves, mention personal symptoms, or say things like "I have been diagnosed", '
        '"I am not a doctor", "I am taking this".\n'
        '- "unchanged": the message gives no clear signal about who the user IS -- it is just a question '
        'or comment without self-identification.\n\n'
        'IMPORTANT: Classify the USER, not the topic. '
        '"What does my doctor think?" means patient. "My patient has a fever" means hcp.\n\n'
        'Conversation so far:\n{conv_str}\n\n'
        'Latest message: {query}'
    )

    chain = ChatPromptTemplate.from_messages([
        ("system", system_text),
        ("human", "{input}"),
    ]) | llm | StrOutputParser()

    try:
        raw = chain.invoke({"input": query, "query": query, "conv_str": conv_str}).strip()
        if raw.startswith('```'):
            raw = raw.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
        result = json.loads(raw)
        journey = result.get("journey", "unchanged")
        if journey in ("hcp", "patient", "unchanged"):
            return journey
    except Exception:
        pass
    return "unchanged"

def _detect_brand(query=''):
    """Return the BRAND_CONFIG entry the user explicitly named in their query.

    Only the patient's own words are checked — retrieved document chunks are
    intentionally ignored.  If the user hasn't mentioned a brand the bot must
    not assume one from the FAISS results; a generic prompt is used instead.
    """
    query_lower = query.lower()
    for brand_key, keywords in _BRAND_KEYWORDS.items():
        if any(kw in query_lower for kw in keywords):
            return BRAND_CONFIG[brand_key]
    return None


def _detect_brand_from_history(conversation_history):
    """Return the brand most recently mentioned in the conversation history.

    Handles both formats the frontend may send:
      - String (formatted text): scan the whole string, return the brand
        whose keyword appears latest (rightmost position).
      - List of dicts/strings: scan the last 6 entries newest-first and
        return on the first brand match.
    """
    if isinstance(conversation_history, str):
        text_lower = conversation_history.lower()
        last_positions = {}
        for brand_key, keywords in _BRAND_KEYWORDS.items():
            pos = max((text_lower.rfind(kw) for kw in keywords), default=-1)
            if pos >= 0:
                last_positions[brand_key] = pos
        if last_positions:
            return BRAND_CONFIG[max(last_positions, key=last_positions.get)]
        return None

    # List format
    for message in reversed(conversation_history[-6:]):
        if isinstance(message, dict):
            text = message.get('content') or message.get('message') or ''
        elif isinstance(message, str):
            text = message
        else:
            continue
        text_lower = text.lower()
        for brand_key, keywords in _BRAND_KEYWORDS.items():
            if any(kw in text_lower for kw in keywords):
                return BRAND_CONFIG[brand_key]
    return None


def _build_hls_brand_prompt(brand, is_first_message=True, brand_switched=False, is_hcp=False, journey_switched=False):
    """Build a brand-aware HLS system prompt.

    Persona names are pinned constants so they never drift across turns or brand switches.
    is_hcp=True  → clinical/MSL tone (Medical Team)
    is_hcp=False → empathetic patient-support tone (Patient Support Team)
    """
    persona    = brand.get('hcp_persona', MEDICAL_TEAM_PERSONA) if is_hcp else brand.get('patient_persona', PATIENT_SUPPORT_PERSONA)
    team_label = 'Medical Team'       if is_hcp else 'Patient Support Team'

    if is_hcp:
        # ── HCP / clinician journey ────────────────────────────────────────
        if is_first_message:
            opening_instruction = (
                'OPENING — First message from this HCP.\n'
                'Introduce yourself as __PERSONA__ from the __NAME__ __TEAM__. '
                'One sentence max. Immediately address their clinical question '
                'with precision and directness.\n\n'
            )
        elif journey_switched:
            opening_instruction = (
                'OPENING - The user has just identified as an HCP.\n'
                'Introduce yourself as __PERSONA__ from the __NAME__ __TEAM__. '
                'Acknowledge the shift - you are now speaking clinician-to-clinician. '
                'Address their clinical question directly.\n\n'
            )
        elif brand_switched:
            opening_instruction = (
                'OPENING - HCP has switched to asking about __NAME__.\n'
                'You are still __PERSONA__ from the __TEAM__. '
                'Briefly acknowledge the topic change and address the new question directly. '
                'Do NOT re-introduce yourself as a new person.\n\n'
            )
        else:
            opening_instruction = (
                'OPENING - Follow-up from an HCP about __NAME__.\n'
                'You are __PERSONA__. Do NOT re-introduce yourself. '
                'Continue with clinical precision.\n\n'
            )

        template = (
            'You are __PERSONA__, a Medical Science Liaison (MSL) from the '
            '__NAME__ __TEAM__ (__AREA__, Aetheris BioGroup). '
            'Tone: clinical, professional, and direct — __TONE__.\n\n'

            '__OPENING__'

            'Reply like a scientific colleague, not a chatbot. '
            'Use clinical terminology appropriate for a physician audience. '
            'Reference trial data, mechanism of action, dosing, or contraindications '
            'from the context where relevant. '
            'Use bullets only when listing 3+ distinct data points; otherwise sentences. '
            'If the HCP needs deeper clinical support beyond what the context provides, '
            'suggest they connect directly with a __NAME__ MSL.\n\n'

            'Use Markdown: \\n\\n between paragraphs, ** for bold, - for bullets. '
            'No headings.\n\n'

            'Context chunks have "page_content" and "metadata" '
            '("filename", "document_unique_id"). Use only what is relevant.\n\n'

            'Suggest 2-3 relevant clinical follow-up questions in "follow_ups".\n\n'

            'Return ONLY valid JSON:\n'
            '{{"answer": "<response>", '
            '"sources": [{{"document_unique_id": "<id>", "page_number": <number or null>}}], '
            '"follow_ups": ["<q1>", "<q2>"], '
            '"show_msl_button": true}}\n'
            'No context match: '
            '{{"answer": "I don\'t have that clinical data available — '
            'connecting with a __NAME__ MSL would be the best next step.", '
            '"sources": [], "follow_ups": [], "show_msl_button": true}}\n'
            'Page number from "--- PAGE X ---" if present, else null.\n'
        )

    else:
        # ── Patient journey ────────────────────────────────────────────────
        if is_first_message:
            opening_instruction = (
                'OPENING — This is the patient\'s first message.\n'
                'Introduce yourself as __PERSONA__ from the __NAME__ __TEAM__. '
                'Sound like a real person on a support call, not a system greeting. '
                'Acknowledge the patient\'s concern with genuine warmth — __EMPATHY__.\n\n'
            )
        elif journey_switched:
            opening_instruction = (
                'OPENING - The user has just identified as a patient.\n'
                'Introduce yourself as __PERSONA__ from the __NAME__ __TEAM__. '
                'Transition warmly into patient support mode - __EMPATHY__.\n\n'
            )
        elif brand_switched:
            opening_instruction = (
                'OPENING - Patient has switched to asking about __NAME__ '
                'after discussing a different brand earlier.\n'
                'You are still __PERSONA__ from the __TEAM__. '
                'Acknowledge the switch naturally - you are the right person '
                'they are now speaking with about __NAME__. '
                'Acknowledge their new concern with warmth - __EMPATHY__.\n\n'
            )
        else:
            opening_instruction = (
                'OPENING - Follow-up in an ongoing conversation about __NAME__.\n'
                'You are __PERSONA__. Do NOT re-introduce yourself. '
                'Acknowledge what the patient said with genuine warmth - __EMPATHY__. '
                'Pick up exactly where the last exchange left off.\n\n'
            )

        template = (
            'You are __PERSONA__, a patient support representative from the '
            '__NAME__ __TEAM__ (__AREA__, Aetheris BioGroup). Tone: __TONE__.\n\n'

            '__OPENING__'

            'Reply like a real person on a support call — not a chatbot. '
            'Be brief and warm. Translate any clinical language into plain words. '
            'Use bullets only for 3+ separate points; otherwise plain sentences. '
            'Always suggest the patient speak with their prescribing physician, '
            'pharmacist, or care nurse for medical decisions. '
            'Close with one warm sentence, like a support person ending a call.\n\n'

            'Use Markdown: \\n\\n between paragraphs, ** for bold, - for bullets. '
            'No headings.\n\n'

            'Context chunks have "page_content" and "metadata" '
            '("filename", "document_unique_id"). Use only what is relevant.\n\n'

            'Suggest 2-3 natural follow-up questions in "follow_ups".\n\n'

            'Return ONLY valid JSON:\n'
            '{{"answer": "<response>", '
            '"sources": [{{"document_unique_id": "<id>", "page_number": <number or null>}}], '
            '"follow_ups": ["<q1>", "<q2>"], '
            '"show_msl_button": false}}\n'
            'No context match: '
            '{{"answer": "I don\'t have that detail right now — '
            'your __NAME__ care team or doctor will be the best people to ask.", '
            '"sources": [], "follow_ups": [], "show_msl_button": false}}\n'
            'Page number from "--- PAGE X ---" if present, else null.\n'
        )

    return (
        template
        .replace('__OPENING__', opening_instruction)
        .replace('__PERSONA__', persona)
        .replace('__TEAM__', team_label)
        .replace('__NAME__', brand['display_name'])
        .replace('__AREA__', brand['therapeutic_area'])
        .replace('__TONE__', brand['tone'])
        .replace('__EMPATHY__', brand.get('empathy_style', ''))
    )


ZENSEAI_SYSTEM_PROMPT = """
You are a ZenseAI Content Factory AI Assistant — a knowledgeable and professional
chatbot that helps users with questions about content, documents, and materials
available in the ZenseAI Content Factory.

Guidelines:
1. Use ONLY the provided context to generate your answer. The context contains document chunks,
   each with a "page_content" field (the text) and a "metadata" field that includes "filename"
   (the source document name) and "document_unique_id" (unique identifier of the source document).
2. If the answer cannot be derived from the context, return:
   {{"answer": "This issue appears to require assistance from our support team.", "sources": [], "follow_ups": [] "Would you like me to create a support ticket?"}}
3. Be professional, concise, and accurate.
4. You MUST respond in valid JSON only. Use this exact format:
   {{"answer": "<your answer text here>", "sources": [{{"document_unique_id": "<id>", "page_number": <number or null>}}], "follow_ups": ["<follow-up question 1>", "<follow-up question 2>", "<follow-up question 3>"]}}
5. In the "sources" array, include ONLY the documents you actually used to form the answer.
   Use the "document_unique_id" from the chunk metadata.
6. If the page_content contains "--- PAGE X ---" markers, extract the page number X from the FIRST marker in the content you used. Otherwise, set page_number to null.
7. Do NOT include any text outside the JSON object. Return ONLY valid JSON.
8. After answering, generate 2-3 relevant follow-up questions the user might naturally ask next,
   based strictly on the answer and retrieved context. Include them in the "follow_ups" array.
   If no meaningful follow-ups exist, set "follow_ups" to [].
"""

DOMAIN_SYSTEM_PROMPTS = {
    'hls': None,
    'zenseai': ZENSEAI_SYSTEM_PROMPT,
}

DOMAIN_GREETINGS = {
    'hls': (
        "Hi there! Thanks for reaching out. I'm here to help with any questions "
        "you have about your medication or treatment — feel free to share "
        "what's on your mind."
    ),
    'zenseai': "Hello! I'm your ZenseAI Content Factory assistant. How can I help you today?",
}

HLS_SYSTEM_PROMPT = """
You are an HLS (Healthcare Life Sciences) AI Assistant — a knowledgeable and professional
chatbot that helps medical professionals and stakeholders with questions about pharmaceutical
products, clinical data, prescribing information, dosage, mechanism of action, adverse
reactions, and more.

Guidelines:
1. Use ONLY the provided context to generate your answer. The context contains document chunks,
   each with a "page_content" field (the text) and a "metadata" field that includes "filename"
   (the source document name) and "document_unique_id" (unique identifier of the source document).
2. If the answer cannot be derived from the context, return:
   {{"answer": "I'm sorry, I don't have that information in my current knowledge base.", "sources": [], "follow_ups": []}}
3. Be professional, concise, and medically accurate.
4. You MUST respond in valid JSON only. Use this exact format:
   {{"answer": "<your answer text here>", "sources": [{{"document_unique_id": "<id>", "page_number": <number or null>}}], "follow_ups": ["<follow-up question 1>", "<follow-up question 2>", "<follow-up question 3>"]}}
5. In the "sources" array, include ONLY the documents you actually used to form the answer.
   Use the "document_unique_id" from the chunk metadata.
6. If the page_content contains "--- PAGE X ---" markers, extract the page number X from the FIRST marker in the content you used. Otherwise, set page_number to null.
7. Do NOT include any text outside the JSON object. Return ONLY valid JSON.
8. After answering, generate 2-3 relevant follow-up questions the user might naturally ask next,
   based strictly on the answer and retrieved context. Include them in the "follow_ups" array.
   If no meaningful follow-ups exist, set "follow_ups" to [].
"""

DOMAIN_SYSTEM_PROMPTS['hls'] = HLS_SYSTEM_PROMPT


async def hls_chat_helper(
    request: Request,
    db: AsyncSession = None
):
    db = AsyncSessionLocal()
    data = await request.json()
    # Prefer the real client IP from proxy headers over the immediate connection host
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        ip_address = forwarded_for.split(",")[0].strip()
    else:
        ip_address = request.headers.get("X-Real-IP") or request.client.host
    if ip_address == '127.0.0.1':
        ip_address = data.get("ip_address", ip_address) 
    result = await process_hls_chat(data, db, ip_address)

    return JSONResponse(
        content=result,
        status_code=200
    )

def _support_agent_llm(
    query,
    conversation_history,
    llm
):

    chain = (
        ChatPromptTemplate.from_messages([
            (
                "system",
                """
You are an empathetic support engineer.

Rules:
1. Understand the user's issue.
2. Ask ONLY ONE troubleshooting question.
3. Use conversation history.
4. Do NOT answer with:
   "I don't have that information".
5. Do NOT give a solution immediately.
6. Investigate first.
7. Continue the conversation naturally.
If:
- user tried password reset
- reset email not received
- troubleshooting has not resolved the issue

then do NOT ask another troubleshooting question.

Respond:

"This issue appears to require investigation by the support team. Would you like me to create a support ticket for you?"

Return no further troubleshooting questions.

Return plain text only.
"""
            ),
            (
                "human",
                """
Conversation History:
{conversation}

Latest User Message:
{input}
"""
            )
        ])
        | llm
        | StrOutputParser()
    )

    return chain.invoke({
        "conversation": str(conversation_history),
        "input": query
    })
    


async def process_hls_chat(
    data,
    db: AsyncSession = None,
    ip_address = None,
):
    db = AsyncSessionLocal()
    try:
        query = data.get('query')
        conversation_history = data.get('conversation_history', [])
        domain = data.get('domain', 'hls')

        if not query:
            return {
                "error": "Missing 'query' parameter."
            }

        llm = await initialize_llm(db, domain)
        system_prompt = DOMAIN_SYSTEM_PROMPTS.get(domain, HLS_SYSTEM_PROMPT)
        greeting = DOMAIN_GREETINGS.get(domain, DOMAIN_GREETINGS['hls'])

        if re.sub(r'\s+', ' ', query.strip().lower()) in greeting_phrases:
            return {
                "response": greeting
            }
        support_keywords = [
            "login",
            "log in",
            "password",
            "qr",
            "qr code",
            "account",
            "access",
            "error",
            "issue",
            "not working",
            "unable",
            "order not delivered",
            "order not shipped",
            "didnt receive my order"
        ]

        if any(k in query.lower() for k in support_keywords):
            support_response = _support_agent_llm(
                query,
                conversation_history,
                llm
            )

            print("SUPPORT_RESPONSE =", support_response)

            return {
                "response": support_response,
                "sources": [],
                "follow_up_questions": []
            }
        # Journey detection: LLM classifies the current message; DB provides the baseline
        # so the session stays in the established journey when the user asks generic questions.
        session_uuid = data.get("session_uuid")
        stored_journey = await _get_stored_journey(session_uuid, db)
        stored_is_hcp = (stored_journey == 'hcp')
        llm_journey = _classify_journey_llm(query, conversation_history, llm)
        if llm_journey in ('hcp', 'patient'):
            is_hcp = (llm_journey == 'hcp')
            journey_switched = (is_hcp != stored_is_hcp)
        else:
            is_hcp = stored_is_hcp
            journey_switched = False

        try:
            refined_query = _refine_query(
                query,
                conversation_history,
                llm
            )

        except Exception as e:
            return {
                "error": f"Error during query refinement: {str(e)}"
            }

        try:
            context, _ = await _find_match(
                refined_query,
                domain,
                db
            )

            print("CONTEXT=", context)

        except Exception as e:
            return {
                "error": f"Error during FAISS search: {str(e)}"
            }

        # Apply a brand-specific prompt only when the user has explicitly named
        # a brand — in the current query or somewhere earlier in the session.
        # NEVER infer a brand from FAISS-retrieved document chunks; doing so
        # would cause the bot to introduce itself as "Maya from Synapta" when
        # the patient just said "I got diarrhea after taking my medicine."
        if domain == 'hls' and not isinstance(context, dict):
            # The frontend appends the current user message to conversation_history
            # before the API call, so we must exclude it when deciding whether the
            # brand was established in a *previous* turn.  Strip the last line
            # (always "user: <current message>") before scanning for prior brand.
            if isinstance(conversation_history, str) and conversation_history:
                last_newline = conversation_history.rfind('\n')
                prior_history = conversation_history[:last_newline] if last_newline >= 0 else ''
            else:
                prior_history = conversation_history
            previous_brand = _detect_brand_from_history(prior_history)
            detected_brand = _detect_brand(refined_query)
            # Active brand: current query wins; fall back to the brand established
            # earlier in this session so follow-up questions stay in context.
            active_brand = detected_brand or previous_brand

            if active_brand:
                is_first_brand_message = previous_brand is None
                brand_switched = (
                    detected_brand is not None and
                    previous_brand is not None and
                    detected_brand['display_name'] != previous_brand['display_name']
                )

                system_prompt = _build_hls_brand_prompt(
                    active_brand, is_first_brand_message, brand_switched, is_hcp,
                    journey_switched=journey_switched,
                )

        try:
            # Augment system prompt to instruct caution around potential adverse events.
            # run quick rules-based scan to decide if we should ask the LLM to be extra careful
            rules_scan = detect_adverse_event(query, conversation_history)
            needs_review = rules_scan.get('needs_adverse_review', False)

            ae_instruction = (
                '\nExtra Adverse Event Handling:\n'
                '- Rules-based pre-scan flagged extra care = NEEDS_REVIEW_VAL.\n'
                '- Catch ALL symptom reports — including soft/indirect ones such as\n'
                '  "after taking the medicine I got diarrhea", "I\'ve been tired since starting it",\n'
                '  "my stomach has been off", "I feel weak", "got a headache after my dose".\n'
                '  These describe real experienced symptoms and must be evaluated.\n'
                '- Do NOT infer an adverse event from a hypothetical or negated statement.\n'
                '- If symptoms are present, recommend seeking medical care as appropriate.\n'
                '- If an adverse event is confirmed or suspected, include this sentence in your\n'
                '  answer: "You can also report this by calling AE_PHONE_VAL."\n'
                '\n'
                'ADVERSE EVENT CLASSIFICATION RULES\n'
                'Mark isAdverseEvent = true ONLY when ALL three are true:\n'
                '  1. The user asserts an actual symptom/event occurred (not hypothetical).\n'
                '  2. The event is associated with use of the product.\n'
                '  3. The symptom is NOT documented in the provided context as a known side effect.\n'
                '\n'
                'Mark isAdverseEvent = false when the user is merely asking about side effects,\n'
                'asking if a symptom is expected, or the symptom IS listed in the context.\n'
                '\n'
                'IMPORTANT OUTPUT FORMAT — return a single JSON object only:\n'
                '{{\n'
                '  "answer": "<human readable answer>",\n'
                '  "sources": [{{"document_unique_id": "<id>", "page_number": <number|null>}}],\n'
                '  "adverse_event_check": {{\n'
                '    "isAdverseEvent": <true|false>,\n'
                '    "isEmergency": <true|false>,\n'
                '    "product_name": "<name or null>",\n'
                '    "event_text": "<extracted sentence or null>",\n'
                '    "actual_event_asserted": <true|false>,\n'
                '    "negated": <true|false>,\n'
                '    "hypothetical": <true|false>,\n'
                '    "confidence": <0.0-1.0>\n'
                '  }},\n'
                '  "follow_ups": ["<q1>", "<q2>"],\n'
                '  "show_msl_button": <true|false>\n'
                '}}\n'
                'Do NOT include text outside the JSON object.\n'
            ).replace('NEEDS_REVIEW_VAL', str(needs_review)).replace('AE_PHONE_VAL', AE_REPORT_PHONE)

            augmented_system_prompt = (system_prompt or '') + "\n\n" + (ae_instruction if domain == 'hls' else "")
            prompt = ChatPromptTemplate.from_messages([
                ("system", augmented_system_prompt),
                ("human", "Query: {input}\nContext: {context}")
            ])

            chain = prompt | llm | StrOutputParser()

            raw_response = chain.invoke({
                "input": refined_query,
                "context": context
            })

        except Exception as e:
            return {
                "error": f"Error generating response: {str(e)}"
            }

        try:
            llm_json = json.loads(raw_response)
            answer = llm_json.get("response") or llm_json.get("answer") or raw_response
            llm_sources = llm_json.get("sources", [])
            follow_up_questions = llm_json.get("follow_ups", [])
            llm_aec = llm_json.get("adverse_event_check")
            show_msl_button = llm_json.get("show_msl_button", is_hcp)

        except (json.JSONDecodeError, TypeError):
            answer = raw_response
            llm_sources = []
            llm_aec = None
            follow_up_questions = []
            show_msl_button = is_hcp

        doc_ids = list({
            source.get("document_unique_id")
            for source in llm_sources
            if source.get("document_unique_id")
        })

        db_sources = await get_sources_from_db_repo(
            db=db,
            doc_ids=doc_ids,
            domain=domain
        )

        page_map = {
            source["document_unique_id"]: source.get("page_number")
            for source in llm_sources
            if source.get("document_unique_id")
        }

        for src in db_sources:
            page = page_map.get(src["document_id"])

            if page is not None:
                src["page_number"] = page

        # Prefer adverse_event_check populated by the LLM. If absent, fall back to rules-based detector.
        adverse_event_check = llm_aec or detect_adverse_event(query, conversation_history)
        get_adverse_event = adverse_event_check.get("isAdverseEvent")
        is_adverse_event = bool(get_adverse_event) if get_adverse_event is not None else False

        # Store chatbot history for hls and zenseai domains
        if domain in ('hls', 'zenseai'):
            try:
                user = data.get("user") or {}
                user_name = user.get("name")
                user_email = user.get("email")

                # session_uuid was resolved at journey-detection time; generate
                # a new one only if the client didn't send one.
                session_uuid = session_uuid or str(_uuid.uuid4())
                now = datetime.utcnow()

                new_turns = [
                    {"role": "user", "content": query, "timestamp": now.isoformat(),
                     "journey": "hcp" if is_hcp else "patient"},
                    {"role": "assistant", "content": answer, "timestamp": now.isoformat()},
                ]

                stmt = select(HLSChatbotHistory).where(HLSChatbotHistory.session_uuid == session_uuid)
                result_row = await db.execute(stmt)
                existing = result_row.scalar_one_or_none()

                if existing:
                    history = json.loads(existing.chat_history or "[]")
                    history.extend(new_turns)
                    existing.chat_history = json.dumps(history)
                    existing.updated_at = now
                    existing.adverse_event = existing.adverse_event or is_adverse_event
                else:
                    record = HLSChatbotHistory(
                        session_uuid=session_uuid,
                        chat_history=json.dumps(new_turns),
                        timestamp=now,
                        user_name=user_name,
                        user_email=user_email,
                        ipaddress=ip_address,
                        domain=domain,
                        adverse_event=is_adverse_event
                    )
                    db.add(record)

                await db.commit()

            except Exception as e:
                return {
                    "error": f"Failed inserting chat response in database {str(e)}"
                }

        return {
            "response": answer,
            "refinedQuery": refined_query,
            "sources": db_sources,
            "adverse_event_check": adverse_event_check,
            "follow_up_questions": follow_up_questions,
            "journey": "hcp" if is_hcp else "patient",
            "show_msl_button": show_msl_button,
        }

    except Exception as e:
        return {
            "error": f"Unexpected server error: {str(e)}"
        }

# ******************************** Helper ***************************
# --- simple rules-based adverse event detector ---
def detect_adverse_event(text: str, convo: str = ''):
    t = (text or '') + ' ' + (convo or '')
    t_low = t.lower()

    ae_keywords = [
        # Severe / emergency
        'allergic', 'allergy', 'anaphylaxis', 'hives', 'swelling',
        'shortness of breath', 'difficulty breathing', 'breathless', 'wheezing',
        'chest pain', 'faint', 'fainted', 'loss of consciousness',
        'seizure', 'convulsion', 'hemorrhage', 'tachycardia', 'edema',
        # GI — frequently mentioned softly
        'diarrhea', 'diarrhoea', 'loose stool', 'stomach ache', 'stomach pain',
        'abdominal pain', 'abdominal cramp', 'stomach cramp', 'upset stomach',
        'constipation', 'bloating', 'indigestion', 'heartburn', 'acid reflux',
        'nausea', 'vomit', 'vomiting',
        # Skin
        'rash', 'itching', 'itch', 'redness', 'bruising', 'blistering',
        # Neurological
        'headache', 'migraine', 'dizziness', 'numbness', 'tingling',
        'tremor', 'trembling', 'shaking', 'confusion', 'memory loss', 'brain fog',
        # Systemic
        'fever', 'chills', 'fatigue', 'tired', 'weakness', 'weak',
        'bleeding', 'palpitations', 'swollen',
        # Musculoskeletal
        'muscle pain', 'muscle ache', 'joint pain', 'back pain', 'cramp', 'cramping',
        # Vision / hearing
        'blurred vision', 'vision change', 'hearing loss', 'tinnitus', 'ringing in ears',
        # General welfare
        'not feeling well', 'feeling sick', 'feeling unwell', 'feel worse', 'feeling worse',
        'weight gain', 'weight loss', 'hair loss', 'alopecia', 'hot flash', 'night sweat',
        'insomnia', 'sleep problem', 'anxiety', 'depression', 'mood change',
        # Labels
        'adverse event', 'side effect', 'side-effect', 'reaction', 'hospital',
    ]

    emergency_keywords = ['anaphylaxis', 'difficulty breathing', 'shortness of breath', 'chest pain', 'loss of consciousness', 'seizure', 'hospital']

    negation_terms = ['no', 'not', 'never', "didn't", "doesn't", "isn't", "wasn't", 'without', 'none']
    hypothetical_terms = ['if', 'could', 'might', 'may', 'would', 'should', 'possible', 'possibly', 'thinking']
    first_person_terms = ['i ', "i'm", "i'm having", 'i am', 'i have', 'i had', 'my ', 'we ', "we're", "our "]

    keyword_matches = []
    for kw in ae_keywords:
        if kw in t_low:
            keyword_matches.append(kw)

    # flag whether rules-based detector notices anything that warrants extra care
    needs_adverse_review = bool(keyword_matches)

    # basic negation detection: look for negation within 5 words before keyword
    negated = False
    for kw in keyword_matches:
        # find all occurrences
        for m in re.finditer(re.escape(kw), t_low):
            start = max(0, m.start() - 100)
            window = t_low[start:m.start()]
            if any(re.search(r'\b' + nt + r'\b', window) for nt in negation_terms):
                negated = True

    # hypothetical detection: presence of hypothetical terms near keyword or overall
    hypothetical = any(re.search(r'\b' + ht + r'\b', t_low) for ht in hypothetical_terms)

    # actual event asserted: basic check for first-person experience verbs near keywords
    actual_event_asserted = False
    if any(fp in t_low for fp in first_person_terms) and keyword_matches and not negated and not hypothetical:
        actual_event_asserted = True

    is_emergency = any(kw in t_low for kw in emergency_keywords)

    # extract simple product name heuristics: look for 'after taking X', 'taking X', 'on X', 'from X'
    product_name = None
    m = re.search(r'(?:after taking|after use of|after using|after|taking|on|from)\s+([A-Za-z0-9\-\_\.]+)', text, re.IGNORECASE)
    if m:
        product_name = m.group(1)
    else:
        # try to find capitalized token near 'drug' or 'medication'
        m2 = re.search(r'(?:drug|medication|product)\s+([A-Za-z0-9\-\_\.]+)', text, re.IGNORECASE)
        if m2:
            product_name = m2.group(1)

    # event_text: best-effort extract sentence containing the keyword
    event_text = None
    if keyword_matches:
        # find first sentence with keyword
        sentences = re.split(r'[\.!?]\s+', text)
        for s in sentences:
            if any(kw in s.lower() for kw in keyword_matches):
                event_text = s.strip()
                break

    # confidence scoring heuristic
    score = 0.0
    if keyword_matches:
        score += 0.4
    if actual_event_asserted:
        score += 0.3
    if is_emergency:
        score += 0.2
    if negated:
        score -= 0.6
    if hypothetical:
        score -= 0.3

    confidence = max(0.0, min(1.0, round(score, 2)))

    isAdverseEvent = bool(keyword_matches) and actual_event_asserted and not negated and not hypothetical and confidence >= 0.2

    return {
        'isAdverseEvent': isAdverseEvent,
        'isEmergency': bool(is_emergency),
        'product_name': product_name,
        'event_text': event_text,
        'actual_event_asserted': actual_event_asserted,
        'negated': negated,
        'hypothetical': hypothetical,
        'confidence': confidence,
        'needs_adverse_review': needs_adverse_review,
    }
