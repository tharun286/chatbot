from __future__ import annotations
import os
import json
import asyncio
import requests
from src.hls_platform.connectors.connector_factory import get_connector
from src.db_session import AsyncSessionLocal
from src.hls_platform.claim_annotations.llm_auto_update_agent import LLMAutoUpdateAgent
from src.hls_platform.claim_annotations.document_claim_updater import (update_docx_claim,update_pptx_claim,update_pdf_claim)
from src.hls_platform.claim_annotations.document_text_extraction_service import (DocumentTextExtractionService,)
import re
import pymupdf
from bs4 import BeautifulSoup
from difflib import SequenceMatcher

# 1. Place this import at the very top of your file
from src.routers.hls_content_factory_routes import AUTO_UPDATE_PROGRESS


# 2. Add this standalone helper function right above the class definition
def update_progress_checkpoint(task_id: str, stage_name: str):
    if not task_id or task_id not in AUTO_UPDATE_PROGRESS:
        return

    current_record = AUTO_UPDATE_PROGRESS[task_id]
    completed = current_record.get("completed_steps", [])

    if stage_name not in completed:
        completed.append(stage_name)

    AUTO_UPDATE_PROGRESS[task_id] = {
        "task_id": task_id,
        "current_step": stage_name,
        "completed_steps": completed,
    }


class AutoUpdateAgent:
    async def get_tagged_text_from_pdf(
        
        self,
        document_id,
        document_name,
        major_version,
        minor_version,
        pdf_path,
        claim_id=None,
        old_claim=None,
        new_claim=None,
        target_new_claim_id=None,
        task_id=None,
        source_file_path=None,
    ):
        """
        Extract annotations directly from an annotated PDF and save them into a txt file.
        """
        print("\n========================")
        print("SOURCE FILE PROCESSING")
        print("========================")
        print(f"SOURCE FILE PATH = {source_file_path}")
        if source_file_path:
            ext = os.path.splitext(source_file_path)[1].lower()
            print(f"FILE EXTENSION = {ext}")
        print("========================\n")
        doc = pymupdf.open(pdf_path)

        text_extractor = DocumentTextExtractionService()
        extracted_document = text_extractor.extract(pdf_path)

        results = []
        old_claim_record_id = None

        connector = get_connector("promomats")

        async with AsyncSessionLocal() as db:
            await connector.load_credentials(db)

        claims_response = connector.get_claims()

        claims_lookup = {
            claim["name"]: claim["id"] for claim in claims_response.get("claims", [])
        }

        has_uploaded_document = False
        latest_upload_context = None 

        docx_updated = False
        docx_upload_result = None
        docx_view_url = None
        ppt_updated = False
        ppt_upload_result = None
        ppt_view_url = None
        pdf_updated = False
        pdf_upload_result = None
        pdf_view_url = None
        try:
            for page_index in range(len(doc)):
                page = doc[page_index]
                annot = page.first_annot

                while annot:
                    try:
                        view_url = None
                        upload_result = None

                        annot_info = annot.info or {}
                        rect = annot.rect

                        x_left = rect.x0
                        y_top = rect.y0
                        x_right = rect.x1
                        y_bottom = rect.y1

                        content = annot_info.get("content", "")

                        annotation_text = re.sub(
                            r"\s+", " ", annot_info.get("subject", "").strip()
                        )

                        text_page_number = None
                        text_start_index = None
                        text_end_index = None

                        try:
                            annotation_tokens = []

                            for token in annotation_text.split():
                                annotation_tokens.extend(
                                    text_extractor._split_vault_token(token)
                                )

                            annotation_tokens = [
                                token.strip()
                                for token in annotation_tokens
                                if token.strip()
                            ]

                            for extracted_page in extracted_document.pages:

                                page_tokens = [
                                    word.text.strip() for word in extracted_page.words
                                ]

                                for start_idx in range(len(page_tokens)):

                                    candidate_tokens = page_tokens[
                                        start_idx : start_idx + len(annotation_tokens)
                                    ]

                                    if candidate_tokens == annotation_tokens:

                                        text_page_number = extracted_page.page_number

                                        text_start_index = extracted_page.words[
                                            start_idx
                                        ].word_index

                                        text_end_index = extracted_page.words[
                                            start_idx + len(annotation_tokens) - 1
                                        ].word_index

                                        break

                                if text_start_index is not None:
                                    break

                        except Exception as e:
                            print(f"⚠️ Failed to calculate " f"text indexes: {e}")

                        match = re.search(r"Claim-\d+", content)

                        annotation_claim_id = match.group(0) if match else ""

                        html_sentence = None
                        html_code = None
                        updated_html_sentence = None
                        updated_html_code = None
                        updated_annotation_text = None
                        html_file_path = None
                        docx_claim_text = None
                        updated_docx_text = None
                        ppt_claim_text = None
                        updated_ppt_text = None
                        pdf_claim_text = None
                        updated_pdf_text = None

                        if annotation_claim_id:

                            html_folder_path = os.path.join(
                                os.path.dirname(
                                    os.path.dirname(os.path.dirname(pdf_path))
                                ),
                                "claim_docs_download_html",
                                os.path.basename(os.path.dirname(pdf_path)),
                            )

                            # 🎯 FIXED: Lookup the local file directly using document_id instead of document_name
                            html_file_path = os.path.join(
                                html_folder_path,
                                f"{document_id}.html",
                            )
                            html_document_name = re.sub(
                                r"\s*\(\d+\.\d+\)$", "", document_name
                            ).strip()
                            file_extension = (
                                os.path.splitext(source_file_path)[1].lower()
                                if source_file_path
                                else ""
                            )
                            if file_extension == ".html":
                               html_file_path = os.path.join(
                                html_folder_path, f"{html_document_name}.html"
                               )
                               print(f"DOCUMENT NAME = {document_name}")
                               print(f"HTML FILE PATH = {html_file_path}")
                               print(f"FILE EXISTS = {os.path.exists(html_file_path)}")
                            elif file_extension == ".docx":
                                print("\n🚨 ENTERED DOCX BRANCH 🚨")
                                print(f"SOURCE FILE = {source_file_path}")
                                print(f"FILE EXTENSION = {file_extension}")
                                print("\n========== DOCX DOCUMENT DETECTED ==========")
                                print(f"DOCUMENT ID = {document_id}")
                                print(f"DOCUMENT NAME = {document_name}")
                                print(f"DOCX FILE = {source_file_path}")
                                print(f"ANNOTATION CLAIM ID = {annotation_claim_id}")
                                print(f"TARGET CLAIM ID = {claim_id}")
                                print("============================================")
                                if annotation_claim_id == claim_id:
                                    docx_claim_text = self.get_docx_claim_text(
                                        file_path=source_file_path,
                                        annotation_text=annotation_text,
                                    )
                                    if docx_claim_text:
                                        print("✅ TARGET CLAIM EXISTS IN DOCX")
                                        print(f"DOCX TEXT = {docx_claim_text}")
                                        if old_claim and new_claim:
                                            print("\n========== CALLING LLM FOR DOCX ==========")
                                            print(f"OLD CLAIM = {old_claim}")
                                            print(f"NEW CLAIM = {new_claim}")
                                            print(f"ANNOTATION TEXT = {annotation_text}")
                                            print(f"DOCX TEXT = {docx_claim_text}")
                                            print("==========================================")
                                            async with AsyncSessionLocal() as db:
                                                agent = LLMAutoUpdateAgent(db)
                                                llm_result = await agent.generate_updated_html_sentence(
                                                    old_claim=old_claim,
                                                    annotation_text=annotation_text,
                                                    html_text=docx_claim_text,
                                                    html_code="",
                                                    new_claim=new_claim,
                                                    document_type="pdf",
                                                )
                                            updated_docx_text = llm_result.get(
                                                "updated_annotation_text"
                                            )
                                            updated_annotation_text = updated_docx_text
                                            print("\n========== DOCX LLM RESULT ==========")
                                            print("CURRENT DOCX TEXT:")
                                            print(docx_claim_text)
                                            print()
                                            print("UPDATED DOCX TEXT:")
                                            print(updated_docx_text)
                                            print("=====================================")
                                            if updated_docx_text:
                                                print("\n========== UPDATING DOCX FILE ==========")
                                                print(f"FILE = {source_file_path}")
                                                print(f"OLD TEXT = {docx_claim_text}")
                                                print(f"NEW TEXT = {updated_docx_text}")


                                                if os.path.exists(html_file_path):
                                                    # 🚀 PATCH 1: Trigger step 8 checkpoint update
                                                    update_progress_checkpoint(
                                                        task_id, "Match Content In HTML"
                                                    )

                                                replacements = update_docx_claim(
                                                    file_path=source_file_path,
                                                    old_claim=docx_claim_text,
                                                    new_claim=updated_docx_text,
                                                )

                                                print(f"DOCX REPLACEMENTS = {replacements}")
                                                print("========================================")

                                                if replacements > 0:
                                                    docx_updated = True
                                                    print("✅ DOCX FILE UPDATED LOCALLY")
                                                else:
                                                    print("❌ DOCX FILE WAS NOT MODIFIED")
                                    else:
                                        print("❌ TARGET CLAIM NOT FOUND IN DOCX")
                            elif file_extension == ".pdf":
                                print("\n========== PDF DOCUMENT DETECTED ==========")
                                print(f"DOCUMENT ID = {document_id}")
                                print(f"DOCUMENT NAME = {document_name}")
                                print(f"PDF FILE = {source_file_path}")
                                print(f"ANNOTATION CLAIM ID = {annotation_claim_id}")
                                print(f"TARGET CLAIM ID = {claim_id}")
                                print("===========================================")
                                if annotation_claim_id == claim_id:
                                    pdf_match = self.get_pdf_claim_text(
                                        file_path=source_file_path,
                                        annotation_text= old_claim or annotation_text,
                                    )
                                    if pdf_match:
                                        print("✅ TARGET CLAIM EXISTS IN PDF")
                                        pdf_claim_text = pdf_match["text"]
                                        print(f"PDF TEXT = {pdf_claim_text}")
                                        if old_claim and new_claim:
                                            print("\n========== CALLING LLM FOR PDF ==========")
                                            print(f"OLD CLAIM = {old_claim}")
                                            print(f"NEW CLAIM = {new_claim}")
                                            print(f"ANNOTATION TEXT = {annotation_text}")
                                            print(f"PDF TEXT = {pdf_claim_text}")
                                            print("=========================================")
                                            async with AsyncSessionLocal() as db:
                                                agent = LLMAutoUpdateAgent(db)
                                                llm_result = await agent.generate_updated_html_sentence(
                                                    old_claim=old_claim,
                                                    annotation_text=annotation_text,
                                                    html_text=pdf_claim_text,
                                                    html_code="",
                                                    new_claim=new_claim,
                                                )
                                            updated_pdf_text = llm_result.get(
                                                "updated_annotation_text"
                                            )
                                            print("\n========== PDF LLM RESULT ==========")
                                            print("CURRENT PDF TEXT:")
                                            print(pdf_claim_text)
                                            print()
                                            print("UPDATED PDF TEXT:")
                                            print(updated_pdf_text)
                                            print("====================================")
                                            original_length = len(pdf_claim_text.strip())
                                            updated_length = len(updated_pdf_text.strip())
                                            print(f"ORIGINAL PDF TEXT LENGTH = {original_length}")
                                            print(f"UPDATED PDF TEXT LENGTH = {updated_length}")
                                            if updated_length > original_length:
                                                print("❌ PDF UPDATE REJECTED")
                                                print("UPDATED PDF TEXT IS LONGER THAN ORIGINAL PDF TEXT")
                                            else:
                                                print("✅ PDF LENGTH VALIDATION PASSED")   
                                            replacements = update_pdf_claim(
                                                file_path=source_file_path,
                                                page_number=pdf_match["page"],
                                                rect=pdf_match["rect"],
                                                old_claim=pdf_claim_text,
                                                new_claim=updated_pdf_text,
                                            )
                                            if replacements > 0:
                                                pdf_updated = True
                                                print(
                                                    "✅ PDF FILE UPDATED LOCALLY"
                                                )
                                            else:
                                                print(
                                                    "❌ PDF FILE WAS NOT MODIFIED"
                                                )
                                            print(f"PDF REPLACEMENTS = {replacements}")
                                            if replacements > 0:
                                                pdf_updated = True
                                                print("✅ PDF FILE UPDATED LOCALLY")
                                            elif replacements == -1:
                                                print(
                                                    "❌ PDF UPDATE FAILED"
                                                )
                                    else:
                                        print("❌ TARGET CLAIM NOT FOUND IN PDF")
                            elif file_extension == ".pptx":
                                print("\n========== PPT DOCUMENT DETECTED ==========")
                                print(f"DOCUMENT ID = {document_id}")
                                print(f"DOCUMENT NAME = {document_name}")
                                print(f"PPT FILE = {source_file_path}")
                                print(f"ANNOTATION CLAIM ID = {annotation_claim_id}")
                                print(f"TARGET CLAIM ID = {claim_id}")
                                print("===========================================")
                                if annotation_claim_id == claim_id:
                                    ppt_claim_text = self.get_ppt_claim_text(
                                        file_path=source_file_path,
                                        annotation_text=annotation_text,
                                    )
                                    if ppt_claim_text:
                                        print("✅ TARGET CLAIM EXISTS IN PPT")
                                        print(f"PPT TEXT = {ppt_claim_text}")
                                        if old_claim and new_claim:
                                            print("\n========== CALLING LLM FOR PPT ==========")
                                            print(f"OLD CLAIM = {old_claim}")
                                            print(f"NEW CLAIM = {new_claim}")
                                            print(f"ANNOTATION TEXT = {annotation_text}")
                                            print(f"PPT TEXT = {ppt_claim_text}")
                                            print("=========================================")
                                            async with AsyncSessionLocal() as db:
                                                agent = LLMAutoUpdateAgent(db)
                                                llm_result = await agent.generate_updated_html_sentence(
                                                    old_claim=old_claim,
                                                    annotation_text=annotation_text,
                                                    html_text=ppt_claim_text,
                                                    html_code="",
                                                    new_claim=new_claim,
                                                )
                                            updated_ppt_text = llm_result.get(
                                                "updated_annotation_text"
                                            )
                                            print("\n========== PPT LLM RESULT ==========")
                                            print("CURRENT PPT TEXT:")
                                            print(ppt_claim_text)
                                            print()
                                            print("UPDATED PPT TEXT:")
                                            print(updated_ppt_text)
                                            print("====================================")
                                            if updated_ppt_text:
                                                print("\n========== UPDATING PPT FILE ==========")
                                                print(f"FILE = {source_file_path}")
                                                print(f"OLD TEXT = {ppt_claim_text}")
                                                print(f"NEW TEXT = {updated_ppt_text}")
                                                replacements = update_pptx_claim(
                                                    file_path=source_file_path,
                                                    old_claim=ppt_claim_text,
                                                    new_claim=updated_ppt_text,
                                                )
                                                print(f"PPT REPLACEMENTS = {replacements}")
                                                print("======================================")
                                                if replacements > 0:
                                                    ppt_updated = True
                                                    print("✅ PPT FILE UPDATED LOCALLY")
                                                else:
                                                  print("❌ PPT FILE WAS NOT MODIFIED")
                            

                                    else:
                                        print("❌ TARGET CLAIM NOT FOUND IN PPT")
                            if html_file_path and os.path.exists(html_file_path):
                                print(
                                    f"Searching HTML sentence in: "
                                    f"{document_name}.html"
                                )
                                html_result = self.get_html_sentence(
                                    claim_id=annotation_claim_id,
                                    annotation_text=annotation_text,
                                    html_file_path=html_file_path,
                                )
                                html_sentence = None
                                html_code = None

                                if html_result:
                                    html_sentence = html_result.get("html_text")
                                    html_code = html_result.get("html_code")

                            if (
                                annotation_claim_id == claim_id
                                and html_sentence
                                and old_claim
                                and new_claim
                            ):

                                async with AsyncSessionLocal() as db:

                                    agent = LLMAutoUpdateAgent(db)

                                    llm_result = (
                                        await agent.generate_updated_html_sentence(
                                            old_claim=old_claim,
                                            annotation_text=annotation_text,
                                            html_text=html_sentence,
                                            html_code=html_code,
                                            new_claim=new_claim,
                                        )
                                    )

                                    updated_annotation_text = llm_result.get(
                                        "updated_annotation_text"
                                    )

                                    updated_html_sentence = llm_result.get(
                                        "updated_html_text"
                                    )

                                    updated_html_code = llm_result.get(
                                        "updated_html_code"
                                    )

                                    # 🚀 PATCH 2: Trigger step 9 context milestone update
                                    update_progress_checkpoint(
                                        task_id, "Generate Updated Content"
                                    )

                                    if updated_html_code:
                                        # 🚀 PATCH 3: Trigger step 10 application write milestone update
                                        update_progress_checkpoint(
                                            task_id, "Apply Document Updates"
                                        )

                                        self.html_doc_update(
                                            html_file_path=html_file_path,
                                            original_html_code=html_code,
                                            updated_html_code=updated_html_code,
                                            html_text=html_sentence,
                                        )

                                        old_claim_record_id = claims_lookup.get(
                                            annotation_claim_id
                                        )

                                        if not has_uploaded_document:
                                            # 🚀 PATCH 4: Trigger step 11 upload tracking milestone update
                                            update_progress_checkpoint(
                                                task_id, "Upload Draft Documents"
                                            )

                                            upload_result = await self.upload_updated_document_and_migrate_annotations(
                                                document_id=document_id,
                                                updated_file_path=html_file_path,
                                                source_major=major_version,
                                                source_minor=minor_version,
                                                old_claim_record_id=old_claim_record_id,
                                            )
                                            # ... Rest of state flag tracking [6]
                                            has_uploaded_document = True
                                        else:
                                            upload_result = latest_upload_context

                                        if upload_result and upload_result.get(
                                            "success"
                                        ):
                                            # 🚀 PATCH 5: Trigger step 12 and 13 structural annotations update logs
                                            update_progress_checkpoint(
                                                task_id, "Migrate Claim Annotations"
                                            )
                                            update_progress_checkpoint(
                                                task_id, "Remove Legacy Annotations"
                                            )

                                            compare_result = (
                                                connector.get_document_compare_url(
                                                    document_id=upload_result[
                                                        "document_id"
                                                    ],
                                                    current_major=upload_result[
                                                        "major_version"
                                                    ],
                                                    current_minor=upload_result[
                                                        "minor_version"
                                                    ],
                                                    compare_major=major_version,
                                                    compare_minor=minor_version,
                                                )
                                            )

                                            view_url = compare_result.get("view_url")

                                        print(f"CLAIM NAME = " f"{annotation_claim_id}")

                                        print(
                                            f"OLD CLAIM RECORD ID = "
                                            f"{old_claim_record_id}"
                                        )

                                        print(
                                            f"TARGET NEW CLAIM ID = "
                                            f"{target_new_claim_id}"
                                        )

                                        if upload_result and upload_result.get(
                                            "success"
                                        ):

                                            await self.create_rectangle_annotation(
                                                document_id=upload_result[
                                                    "document_id"
                                                ],
                                                major_version=upload_result[
                                                    "major_version"
                                                ],
                                                minor_version=upload_result[
                                                    "minor_version"
                                                ],
                                                claim_record_id=(
                                                    target_new_claim_id
                                                    if target_new_claim_id
                                                    else old_claim_record_id
                                                ),
                                                page_number=page_index + 1,
                                                x_left=x_left,
                                                y_top=y_top,
                                                x_right=x_right,
                                                y_bottom=y_bottom,
                                                annotation_text=annotation_text,
                                                updated_annotation_text=updated_annotation_text,
                                                text_page_number=text_page_number,
                                                text_start_index=text_start_index,
                                                text_end_index=text_end_index,
                                            )

                                    print(f"✅ Updated HTML generated for {annotation_claim_id}")

                        print("VIEW URL =", view_url)
                        print("UPLOAD RESULT =", upload_result)
                        print(
                            "RESULT OBJECT =",
                            {
                                "document_name": document_name,
                                "view_url": view_url,
                                "uploaded_document_id": (
                                    upload_result.get("document_id")
                                    if upload_result
                                    else None
                                ),
                                "uploaded_major_version": (
                                    upload_result.get("major_version")
                                    if upload_result
                                    else None
                                ),
                                "uploaded_minor_version": (
                                    upload_result.get("minor_version")
                                    if upload_result
                                    else None
                                ),
                            },
                        )

                        results.append(
                            {
                                "claim_id": annotation_claim_id,
                                "page_number": page_index + 1,
                                "document_name": document_name,
                                "view_url": view_url,
                                "uploaded_document_id": (
                                    upload_result.get("document_id")
                                    if upload_result
                                    else None
                                ),
                                "uploaded_major_version": (
                                    upload_result.get("major_version")
                                    if upload_result
                                    else None
                                ),
                                "uploaded_minor_version": (
                                    upload_result.get("minor_version")
                                    if upload_result
                                    else None
                                ),
                            }
                        )

                    except Exception as e:
                        print(
                            f"⚠️ Failed reading annotation "
                            f"on page {page_index + 1}: {e}"
                        )

                    annot = annot.next
        finally:
            doc.close()
        if docx_updated:
            print("\n========== UPLOADING UPDATED DOCX ==========")
            print(f"DOCUMENT ID = {document_id}")
            print(f"DOCX FILE = {source_file_path}")
            print(f"SOURCE VERSION = {major_version}.{minor_version}")
            docx_upload_result = (
                await self.upload_updated_document_and_migrate_annotations(
                    document_id=document_id,
                    updated_file_path=source_file_path,
                    source_major=major_version,
                    source_minor=minor_version,
                )
            )
            print(f"DOCX UPLOAD RESULT = {docx_upload_result}")
            if (
                docx_upload_result
                and docx_upload_result.get("success")
            ):
                print(
                    "✅ DOCX UPLOADED ONCE: "
                    f"{major_version}.{minor_version} -> "
                    f"{docx_upload_result['major_version']}."
                    f"{docx_upload_result['minor_version']}"
                )
                connector = get_connector("promomats")
                async with AsyncSessionLocal() as db:
                    await connector.load_credentials(db)
                compare_result = connector.get_document_compare_url(
                    document_id=docx_upload_result["document_id"],
                    current_major=docx_upload_result["major_version"],
                    current_minor=docx_upload_result["minor_version"],
                    compare_major=major_version,
                    compare_minor=minor_version,
                )
                docx_view_url = compare_result.get("view_url")
                print(f"DOCX COMPARE URL = {docx_view_url}")
                print("\n========== UPDATING DOCX RESULTS ==========")
                print(f"TARGET CLAIM ID = {claim_id}")
                print(f"TOTAL RESULTS = {len(results)}")
                for result in results:
                    if result.get("claim_id") == claim_id:
                        result["uploaded_document_id"] = docx_upload_result["document_id"]
                        result["uploaded_major_version"] = docx_upload_result["major_version"]
                        result["uploaded_minor_version"] = docx_upload_result["minor_version"]
                        result["view_url"] = docx_view_url
            else:
                print("❌ DOCX UPLOAD FAILED")
        if pdf_updated:
            print("\n========== UPLOADING UPDATED PDF ==========")
            print(f"DOCUMENT ID = {document_id}")
            print(f"PDF FILE = {source_file_path}")
            print(f"SOURCE VERSION = {major_version}.{minor_version}")
            pdf_upload_result = (
                await self.upload_updated_document_and_migrate_annotations(
                    document_id=document_id,
                    updated_file_path=source_file_path,
                    source_major=major_version,
                    source_minor=minor_version,
                )
            )
            print(f"PDF UPLOAD RESULT = {pdf_upload_result}")
            if (
                pdf_upload_result
                and pdf_upload_result.get("success")
            ):
                print(
                    "✅ PDF UPLOADED ONCE: "
                    f"{major_version}.{minor_version} -> "
                    f"{pdf_upload_result['major_version']}."
                    f"{pdf_upload_result['minor_version']}"
                )
                connector = get_connector("promomats")
                async with AsyncSessionLocal() as db:
                    await connector.load_credentials(db)
                compare_result = connector.get_document_compare_url(
                    document_id=pdf_upload_result["document_id"],
                    current_major=pdf_upload_result["major_version"],
                    current_minor=pdf_upload_result["minor_version"],
                    compare_major=major_version,
                    compare_minor=minor_version,
                )
                pdf_view_url = compare_result.get("view_url")
                print(f"PDF COMPARE URL = {pdf_view_url}")
                print("\n========== UPDATING PDF RESULTS ==========")
                for result in results:
                    if result.get("claim_id") == claim_id:
                        result["uploaded_document_id"] = (
                        pdf_upload_result["document_id"]
                        )
                        result["uploaded_major_version"] = (
                        pdf_upload_result["major_version"]
                        )
                        result["uploaded_minor_version"] = (
                        pdf_upload_result["minor_version"]
                        )
                        result["view_url"] = pdf_view_url
                else:
                   print("❌ PDF UPLOAD FAILED")
        if ppt_updated:
            print("\n========== UPLOADING UPDATED PPT ==========")
            print(f"DOCUMENT ID = {document_id}")
            print(f"PPT FILE = {source_file_path}")
            print(f"SOURCE VERSION = {major_version}.{minor_version}")
            ppt_upload_result = (
                await self.upload_updated_document_and_migrate_annotations(
                    document_id=document_id,
                    updated_file_path=source_file_path,
                    source_major=major_version,
                    source_minor=minor_version,
                )
            )
            print(f"PPT UPLOAD RESULT = {ppt_upload_result}")
            if (
                ppt_upload_result
                and ppt_upload_result.get("success")
            ):
                print(
                    "✅ PPT UPLOADED ONCE: "
                    f"{major_version}.{minor_version} -> "
                    f"{ppt_upload_result['major_version']}."
                    f"{ppt_upload_result['minor_version']}"
                )
                connector = get_connector("promomats")
                async with AsyncSessionLocal() as db:
                    await connector.load_credentials(db)
                compare_result = connector.get_document_compare_url(
                    document_id=ppt_upload_result["document_id"],
                    current_major=ppt_upload_result["major_version"],
                    current_minor=ppt_upload_result["minor_version"],
                    compare_major=major_version,
                    compare_minor=minor_version,
                )
                ppt_view_url = compare_result.get("view_url")
                print(f"PPT COMPARE URL = {ppt_view_url}")
                print("\n========== UPDATING PPT RESULTS ==========")
                for result in results:
                    if result.get("claim_id") == claim_id:
                        result["uploaded_document_id"] = (
                            ppt_upload_result["document_id"]
                        )
                        result["uploaded_major_version"] = (
                            ppt_upload_result["major_version"]
                        )
                        result["uploaded_minor_version"] = (
                            ppt_upload_result["minor_version"]
                        )
                        result["view_url"] = ppt_view_url
            else:
                print("❌ PPT UPLOAD FAILED")
        safe_document_name = re.sub(r'[<>:"/\\|?*]', "_", document_name)

        txt_file_path = os.path.join(
            os.path.dirname(pdf_path), f"tagged_text_{safe_document_name}.txt"
        )
        with open(txt_file_path, "w", encoding="utf-8") as txt_file:

            txt_file.write(f"DOCUMENT ID: {document_id}\n")
            txt_file.write(f"VERSION: {major_version}.{minor_version}\n")
            txt_file.write("=" * 100 + "\n\n")
            if not results:
                txt_file.write("NO ANNOTATIONS FOUND\n")

            for item in results:

                txt_file.write(f"CLAIM ID: {item.get('claim_id')}\n")

                txt_file.write(f"OLD CLAIM RECORD ID: " f"{old_claim_record_id}\n")

                txt_file.write(f"TARGET NEW CLAIM ID: " f"{target_new_claim_id}\n")

                txt_file.write(f"ANNOTATION TEXT: " f"{item.get('annotation_text')}\n")

                txt_file.write(
                    f"UPDATED ANNOTATION TEXT: "
                    f"{item.get('updated_annotation_text')}\n"
                )

                txt_file.write(f"HTML SENTENCE: " f"{item.get('html_sentence')}\n")

                txt_file.write(
                    f"UPDATED HTML SENTENCE: " f"{item.get('updated_html_sentence')}\n"
                )

                txt_file.write(f"DOCUMENT NAME: " f"{item.get('document_name')}\n")

                txt_file.write(f"VIEW URL: " f"{item.get('view_url')}\n")

                # txt_file.write(f"\nCONTENT:\n{item['content']}\n")
                txt_file.write("-" * 100 + "\n\n")

                

        print(f"✅ Tagged text file created: {txt_file_path}")
        print("\n========== FINAL RESULTS ==========")
        for result in results:
            print({
                "claim_id": result.get("claim_id"),
                "document_name": result.get("document_name"),
                "uploaded_document_id": result.get("uploaded_document_id"),
                "uploaded_major_version": result.get("uploaded_major_version"),
                "uploaded_minor_version": result.get("uploaded_minor_version"),
                "view_url": result.get("view_url"),
            })
        print("==================================")
        return {
            "document_id": document_id,
            "pdf_path": pdf_path,
            "txt_file_path": txt_file_path,
            "results": results,
        }

    def get_docx_claim_text(
        self,
        file_path: str,
        annotation_text: str,
    ) -> str | None:
        """
        Find the DOCX paragraph containing the annotation text.
        """
        if not file_path:
            print("❌ DOCX file path is empty")
            return None
        if not os.path.exists(file_path):
            print(f"❌ DOCX file does not exist: {file_path}")
            return None
        if not annotation_text:
            print("❌ Annotation text is empty")
            return None
        try:
            from docx import Document
            document = Document(file_path)
            normalized_annotation = re.sub(
                r"\s+",
                " ",
                annotation_text.strip(),
            ).lower()
            print("\n========== SEARCHING DOCX ==========")
            print(f"DOCX FILE = {file_path}")
            print(f"ANNOTATION = {annotation_text}")
            print("\n========== SEARCH STRING ==========")
            print(repr(annotation_text))
            print("===================================")
            doc = Document(file_path)
            print("\n========== DOCX PARAGRAPHS ==========")
            for para in doc.paragraphs:
                if para.text.strip():
                    print(repr(para.text))
            print("=====================================")
            from difflib import SequenceMatcher
            best_match = None
            best_score = 0
            for paragraph in document.paragraphs:
                paragraph_text = re.sub(
                    r"\s+",
                    " ",
                    paragraph.text.strip(),
                )
                if not paragraph_text:
                    continue
                normalized_paragraph = paragraph_text.lower()
                score = SequenceMatcher(
                    None,
                    normalized_annotation,
                    normalized_paragraph,
                ).ratio()
                print(
                    f"MATCH SCORE = {score:.2f} | {paragraph_text[:100]}"
                )
                if score > best_score:
                    best_score = score
                    best_match = paragraph_text
            print(f"BEST SCORE = {best_score}")
            print(f"BEST MATCH = {best_match}")
            if best_match:
                print("✅ DOCX MATCH FOUND")
                print(f"BEST SCORE = {best_score}")
                print(f"PARAGRAPH = {best_match}")
                return best_match
            print("❌ DOCX MATCH NOT FOUND")
            return None
        except Exception as e:
            print(f"❌ Error searching DOCX: {e}")
            return None
    def get_pdf_claim_text(
        self,
        file_path: str,
        annotation_text: str,
    ):
        """
        Find the PDF text block that best matches the annotation text.
        """
        if not file_path:
            print("❌ PDF file path is empty")
            return None
        if not os.path.exists(file_path):
            print(f"❌ PDF file does not exist: {file_path}")
            return None
        if not annotation_text:
            print("❌ Annotation text is empty")
            return None
        try:
            import fitz
            from difflib import SequenceMatcher
            document = fitz.open(file_path)
            normalized_annotation = re.sub(
                r"\s+",
                " ",
                annotation_text.strip(),
            ).lower()
            best_match = None
            best_score = 0
            print("\n========== SEARCHING PDF ==========")
            print(f"PDF FILE = {file_path}")
            print(f"ANNOTATION = {annotation_text}")
            print("===================================")
            for page_index in range(len(document)):
                page = document[page_index]
                blocks = page.get_text("blocks")
                for block in blocks:
                    block_text = block[4].strip()
                    if not block_text:
                        continue
                    normalized_block = re.sub(
                        r"\s+",
                        " ",
                        block_text,
                    ).lower()
                    normalized_block = normalized_block.replace(
                        "&lt;",
                        "<",
                    )
                    normalized_annotation = normalized_annotation.replace(
                        "&lt;",
                        "<",
                    )
                    score = SequenceMatcher(
                        None,
                        normalized_annotation,
                        normalized_block,
                    ).ratio()
                    print(
                        f"PAGE={page_index + 1} "
                        f"SCORE={score:.2f} "
                        f"TEXT={block_text[:120]}"
                    )
                    if score > best_score:
                        best_score = score
                        best_match = {
                            "page": page_index,
                            "rect": fitz.Rect(
                                block[0],
                                block[1],
                                block[2],
                                block[3],
                            ),
                            "text": block_text,
                        }
            document.close()
            print("\n========== PDF BEST MATCH ==========")
            print(f"BEST SCORE = {best_score}")
            if best_match:
                print(f"BEST MATCH = {best_match['text']}")
            print("====================================")
            if best_match:
                print("✅ PDF MATCH FOUND")
                return best_match
            print("❌ PDF MATCH NOT FOUND")
            return None
        except Exception as e:
            print(f"❌ Error searching PDF: {e}")
            return None
    def get_ppt_claim_text(
        self,
        file_path: str,
        annotation_text: str,
    ) -> str | None:
        """
        Find the PPT text that best matches the annotation text.
        """
        if not file_path:
            print("❌ PPT file path is empty")
            return None
        if not os.path.exists(file_path):
            print(f"❌ PPT file does not exist: {file_path}")
            return None
        if not annotation_text:
            print("❌ Annotation text is empty")
            return None
        try:
            from pptx import Presentation
            from difflib import SequenceMatcher
            import re
            presentation = Presentation(file_path)
            normalized_annotation = re.sub(
                r"\s+",
                " ",
                annotation_text.strip(),
            ).lower()
            best_match = None
            best_score = 0
            print("\n========== SEARCHING PPT ==========")
            print(f"PPT FILE = {file_path}")
            print(f"ANNOTATION = {annotation_text}")
            print("===================================")
            for slide_index, slide in enumerate(presentation.slides, start=1):
                for shape in slide.shapes:
                    if not getattr(shape, "has_text_frame", False):
                        continue
                    shape_text = shape.text.strip()
                    if not shape_text:
                        continue
                    normalized_shape_text = re.sub(
                        r"\s+",
                        " ",
                        shape_text,
                    ).lower()
                    score = SequenceMatcher(
                        None,
                        normalized_annotation,
                        normalized_shape_text,
                    ).ratio()
                    print(
                        f"SLIDE={slide_index} "
                        f"SCORE={score:.2f} "
                        f"TEXT={shape_text[:120]}"
                    )
                    if score > best_score:
                        best_score = score
                        best_match = shape_text
            print("\n========== PPT BEST MATCH ==========")
            print(f"BEST SCORE = {best_score}")
            print(f"BEST MATCH = {best_match}")
            print("====================================")
            if best_match:
                print("✅ PDF MATCH FOUND")
                print(f"BEST SCORE = {best_score}")
                print(f"BEST TEXT = {best_match['text']}")
                return best_match
            print("❌ PDF MATCH NOT FOUND")
            return None
        except Exception as e:
            print(f"❌ Error searching PPT: {e}")
            return None
    def get_html_sentence(
        self,
        claim_id: str,
        annotation_text: str,
        html_file_path: str,
    ) -> dict | None:
        """
        Find the HTML paragraph that contains the annotation text.
        Return both plain text and the original HTML code.
        """
        if not claim_id:
            print("⚠️ Skipping annotation because claim_id is empty")
            return None

        if not annotation_text:
            print(f"⚠️ Empty annotation text for {claim_id}")
            return None

        if not os.path.exists(html_file_path):
            print(f"❌ HTML file does not exist: " f"{html_file_path}")
            return None

        annotation_text = (
            annotation_text.replace("+", " + ").replace("(", " ").replace(")", " ")
        )

        normalized_annotation = re.sub(r"\s+", " ", annotation_text.lower().strip())

        try:

            with open(html_file_path, "r", encoding="utf-8") as f:
                html_content = f.read()

            soup = BeautifulSoup(html_content, "html.parser")

            annotation_words = set(normalized_annotation.split())

            paragraphs = soup.find_all("p")

            for paragraph in paragraphs:

                paragraph_text = paragraph.get_text(separator=" ", strip=True)

                normalized_paragraph = re.sub(
                    r"\s+", " ", paragraph_text.lower().strip()
                )

                normalized_paragraph = (
                    normalized_paragraph.replace("+", " + ")
                    .replace("(", " ")
                    .replace(")", " ")
                )

                paragraph_words = set(normalized_paragraph.split())

                overlap = len(annotation_words.intersection(paragraph_words))

                required_overlap = max(3, int(len(annotation_words) * 0.6))

                if (
                    normalized_annotation in normalized_paragraph
                    or overlap >= required_overlap
                ):

                    print(f"✅ Found matching paragraph " f"for {claim_id}")

                    return {
                        "html_text": paragraph_text.strip(),
                        "html_code": str(paragraph),
                    }

            if claim_id == "Claim-000001":

                print("\n=== DEBUG CLAIM-000001 ===")
                print("ANNOTATION:")
                print(normalized_annotation)

                print("\nPARAGRAPHS:")

                for paragraph in paragraphs:

                    print(paragraph.get_text(separator=" ", strip=True)[:300])

        except Exception as e:

            print(f"⚠️ Error processing " f"{html_file_path}: {e}")

        print(f"❌ No matching paragraph found " f"for {claim_id}")

        return None

    def html_doc_update(
        self,
        html_file_path: str,
        original_html_code: str,
        updated_html_code: str,
        html_text: str = None,
    ) -> bool:

        try:
            with open(html_file_path, "r", encoding="utf-8") as f:
                html_content = f.read()

            # --------------------------------------------------
            # NO CHANGE DETECTED
            # --------------------------------------------------
            if (
                not updated_html_code
                or not original_html_code
                or original_html_code.strip() == updated_html_code.strip()
            ):
                print("✅ No HTML changes detected. Skipping update.")
                return False

            soup = BeautifulSoup(html_content, "html.parser")

            target_text = re.sub(r"\s+", " ", (html_text or "").lower().strip())

            best_paragraph = None
            best_score = 0

            # --------------------------------------------------
            # FIND ACTUAL PARAGRAPH FROM HTML FILE
            # --------------------------------------------------
            for paragraph in soup.find_all("p"):

                paragraph_text = paragraph.get_text(separator=" ", strip=True)

                normalized_paragraph = re.sub(
                    r"\s+", " ", paragraph_text.lower().strip()
                )

                score = SequenceMatcher(None, target_text, normalized_paragraph).ratio()

                if score > best_score:
                    best_score = score
                    best_paragraph = paragraph

            print(f"BEST MATCH SCORE = {best_score}")

            # --------------------------------------------------
            # STRICT MATCHING
            # --------------------------------------------------
            if best_score < 0.95:
                print(
                    f"❌ Similarity score too low ({best_score}). " f"Skipping update."
                )
                return False

            # --------------------------------------------------
            # REPLACE PARAGRAPH SAFELY
            # --------------------------------------------------
            replacement_soup = BeautifulSoup(updated_html_code, "html.parser")

            replacement_paragraph = replacement_soup.find("p")

            if replacement_paragraph is None:
                print("❌ Updated HTML does not contain a paragraph.")
                return False

            best_paragraph.replace_with(replacement_paragraph)

            with open(html_file_path, "w", encoding="utf-8") as f:
                f.write(str(soup))

            print(f"✅ HTML updated successfully " f"(similarity={best_score})")

            return True

        except Exception as e:
            print(f"❌ Error updating HTML: {e}")
            return False

    async def create_rectangle_annotation(
        self,
        document_id: str,
        major_version: int,
        minor_version: int,
        claim_record_id: str,
        page_number: int,
        x_left: float,
        y_top: float,
        x_right: float,
        y_bottom: float,
        annotation_text: str = None,
        updated_annotation_text: str = None,
        text_page_number: int = None,
        text_start_index: int = None,
        text_end_index: int = None,
    ):
        try:
            connector = get_connector("promomats")

            async with AsyncSessionLocal() as db:
                await connector.load_credentials(db)

            _, headers, base_url = connector.authenticate()
            headers["Content-Type"] = "application/json"
            headers["Accept"] = "application/json"

            base_url = "https://partnersi-coeus-bridgeview-promomats.veevavault.com"

            annotation_url = f"{base_url}/api/v26.2/objects/documents/annotations/batch"

            document_version_id = f"{document_id}_{major_version}_{minor_version}"

            print("\n==============================")
            print("CREATING ANNOTATION")
            print("==============================")
            print(f"DOCUMENT VERSION = {document_version_id}")
            print(f"CLAIM RECORD ID  = {claim_record_id}")
            print(f"ANNOTATION WILL LINK TO CLAIM = " f"{claim_record_id}")
            print(f"TEXT PAGE        = {text_page_number}")
            print(f"TEXT START       = {text_start_index}")
            print(f"TEXT END         = {text_end_index}")

            # --------------------------------------------------
            # Preferred Path : Text-Based Annotation
            # --------------------------------------------------
            if (
                text_page_number is not None
                and text_start_index is not None
                and text_end_index is not None
            ):

                payload = [
                    {
                        "document_version_id__sys": document_version_id,
                        "type__sys": "keyword_link__sys",
                        "linked_records__sys": [claim_record_id],
                        "state__sys": "open__sys",
                        "prevent_bring_forward__sys": False,
                        "placemark": {
                            "type__sys": "text__sys",
                            "page_number__sys": text_page_number,
                            "text_start_index__sys": text_start_index,
                            "text_end_index__sys": text_end_index,
                            "style__sys": "text_link__sys",
                        },
                    }
                ]

                print("✅ USING TEXT-BASED ANNOTATION")

            else:

                width = x_right - x_left
                height = y_bottom - y_top

                payload = [
                    {
                        "document_version_id__sys": document_version_id,
                        "type__sys": "keyword_link__sys",
                        "linked_records__sys": [claim_record_id],
                        "state__sys": "open__sys",
                        "prevent_bring_forward__sys": False,
                        "placemark": {
                            "type__sys": "rectangle__sys",
                            "page_number__sys": page_number,
                            "x_coordinate__sys": x_left,
                            "y_coordinate__sys": y_top,
                            "width__sys": width,
                            "height__sys": height,
                            "style__sys": "rectangle_solid__sys",
                        },
                    }
                ]

                print("⚠️ FALLBACK TO RECTANGLE ANNOTATION")

            print("\nPAYLOAD:")
            print(json.dumps(payload, indent=2))

            response = requests.post(
                annotation_url,
                headers=headers,
                json=payload,
                verify=False,
                timeout=120,
            )

            print(f"ANNOTATION STATUS = {response.status_code}")

            try:
                print(json.dumps(response.json(), indent=2))
            except Exception:
                print(response.text)

            return response.json()

        except Exception as e:
            print(f"❌ create_rectangle_annotation failed: {e}")
            return None

    async def upload_updated_document_and_migrate_annotations(
        self,
        document_id: str,
        updated_file_path: str,
        source_major: int = 0,
        source_minor: int = 1,
        old_claim_record_id: str = None,
    ):
        try:
            print("\n===================================================")
            print("UPLOAD UPDATED DOCUMENT TO VEEVA")
            print("===================================================")

            connector = get_connector("promomats")

            async with AsyncSessionLocal() as db:
                await connector.load_credentials(db)

            _, headers, base_url = connector.authenticate()

            print(f"📄 Document ID : {document_id}")
            print(f"📄 File Path   : {updated_file_path}")

            # ==========================================================
            # STEP 1 : Upload Updated HTML As New Version
            # ==========================================================

            version_url = (
                f"{base_url}/objects/documents/{document_id}"
                f"?createDraft=uploadedContent"
            )

            with open(updated_file_path, "rb") as file_obj:

                files = {
                    "file": (
                        os.path.basename(updated_file_path),
                        file_obj,
                    )
                }

                upload_response = requests.post(
                    version_url,
                    headers=headers,
                    files=files,
                    verify=False,
                    timeout=120,
                )

            print(f"Upload Status = {upload_response.status_code}")

            try:
                upload_json = upload_response.json()
                print(json.dumps(upload_json, indent=2))
            except Exception:
                print("❌ Upload response is not JSON")
                print(upload_response.text)

                return {"success": False, "error": "Upload response is not JSON"}

            if (
                upload_response.status_code not in [200, 201]
                or upload_json.get("responseStatus") != "SUCCESS"
            ):
                print("❌ Upload Failed")

                return {"success": False, "error": "Upload Failed"}

            major = upload_json.get("major_version_number__v", 0)
            minor = upload_json.get("minor_version_number__v", 0)

            print(f"\n✅ New Version Created : {major}.{minor}")

            # ==========================================================
            # STEP 2 : Wait For Draft Creation
            # ==========================================================

            print("\n⏳ Waiting 10 seconds...")
            await asyncio.sleep(10)

            # ==========================================================
            # STEP 3 : BRING FORWARD ANNOTATIONS
            # ==========================================================

            print(
                f"\n📥 Copying annotations "
                f"V{source_major}.{source_minor} "
                f"→ "
                f"V{major}.{minor}"
            )

            vault_base_url = base_url.split("/api/")[0]

            bfa_url = f"{vault_base_url}/ui/bfa/execute"

            payload = {
                "docId": str(document_id),
                "majorFrom": str(source_major),
                "minorFrom": str(source_minor),
                "majorTo": str(major),
                "minorTo": str(minor),
                "lineenabled": "true",
                "linkenabled": "true",
                "noteenabled": "true",
                "anchorenabled": "true",
                "resolvednoteenabled": "false",
                "autolinkenabled": "false",
                "noPageLevel": "true",
                # "noDuplicate": "true",
            }

            bfa_headers = headers.copy()
            bfa_headers["X-Requested-With"] = "XMLHttpRequest"
            bfa_headers["Content-Type"] = "application/x-www-form-urlencoded"

            response = requests.post(
                bfa_url,
                headers=bfa_headers,
                data=payload,
                verify=False,
                timeout=120,
            )

            print(f"BFA Status = {response.status_code}")

            try:
                print(json.dumps(response.json(), indent=2))
            except Exception:
                print(response.text)

            print(
                f"\n✅ Successfully uploaded and migrated annotations "
                f"for document {document_id}"
            )

            return {
                "success": True,
                "document_id": document_id,
                "major_version": major,
                "minor_version": minor,
            }

        except Exception as e:

            print(
                f"\n❌ upload_updated_document_and_migrate_annotations " f"failed: {e}"
            )

            return {
                "success": False,
                "error": str(e),
            }
