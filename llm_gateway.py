import re
import json
import requests
from datetime import datetime
from mitmproxy import http, ctx

GUARDIAN_API = "http://guardian:11434/api/chat"
TOXICITY_THRESHOLD = 0.7 # UNUSED - Ollama returns text verdicts, not scores
BLOCK_REASONS = {
    "1": "Description of violent acts",
    "2": "Inquiries on how to perform an illegal activity",
    "3": "Any sexual content",
    "toxicity": "AI-detected toxicity",
}

def check_guardian(prompt: str) -> bool:
    resp = requests.post(GUARDIAN_API, json={
        "model": "granite3-guardian:2b",
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0}
    }, timeout=60)

    content = resp.json().get("message", {}).get("content", "").lower()
    ctx.log.info(f"[GUARDIAN] Response: '{content}'")
    return any(word in content for word in ["unsafe", "harmful", "toxic", "yes"])


def keyword_reason(prompt: str) -> str | None:
    p = prompt.lower()
    if re.search(r'\b(kill|stab|attack|knife|punch)\b', p):
        return "1"
    if re.search(r'\b(steal|hack|illegal|rob)\b', p):
        return "2"
    if re.search(r'\bsex\w*\b', p) or re.search(r'\b(porn|nsfw|erotic)\b', p):
        return "3"
    return None


def log(direction: str, label: str, content: str, extra: str = ""):
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    snippet = content[:120].replace("\n", " ")
    suffix = f" | {extra}" if extra else ""
    ctx.log.info(f"[{ts}] [{direction}] {label}: \"{snippet}\"{suffix}")


class LLMGateway:
    # ------------------------------------------------------------------
    # REQUEST INTERCEPTION
    # ------------------------------------------------------------------
    def request(self, flow: http.HTTPFlow):
        if "v1/chat/completions" not in flow.request.pretty_url:
            log("REQUEST", "SKIPPED", flow.request.pretty_url, extra="non-LLM endpoint")
            return

        data = json.loads(flow.request.content)
        user_prompt = data["messages"][-1]["content"]

        log("REQUEST", "PROMPT", user_prompt)

        reason = None

        # 1. AI toxicity check via Granite Guardian (Ollama)
        try:
            if check_guardian(user_prompt):
                reason = "toxicity"
        except Exception as e:
            ctx.log.warn(f"[GUARDIAN] AI check failed: {e}")

        # 2. Simple method to classify 1,2,3 reasons
        keyword_hit = keyword_reason(user_prompt)
        if keyword_hit:
            reason = keyword_hit

        if reason:
            label = BLOCK_REASONS.get(reason, reason)
            log("REQUEST", "BLOCKED", user_prompt, extra=f"reason={label}")

            if reason == "toxicity":
                msg = "the prompt is considered toxic"
            else:
                msg = f"The prompt was blocked because it contained {reason}"

            flow.response = http.Response.make(
                200,
                json.dumps({
                    "choices": [{"message": {"role": "assistant", "content": msg}}]
                }),
                {"Content-Type": "application/json"},
            )
            
            flow.metadata["gateway_blocked"] = True
        else:
            log("REQUEST", "FORWARDED", user_prompt)
            flow.request.headers["Host"] = "api.openai.com"
            flow.request.host = "api.openai.com"
            flow.request.scheme = "https"
            flow.request.port = 443

    # ------------------------------------------------------------------
    # RESPONSE MONITORING
    # ------------------------------------------------------------------
    def response(self, flow: http.HTTPFlow):
        if "v1/chat/completions" not in flow.request.pretty_url:
            log("RESPONSE", "SKIPPED", flow.request.pretty_url, extra="non-LLM endpoint")
            return

        if flow.metadata.get("gateway_blocked"):
            log("RESPONSE", "SKIPPED", flow.request.pretty_url, extra="already blocked by gateway — no response check needed")
            return

        try:
            body = json.loads(flow.response.content)
            choices = body.get("choices", [])
            if not choices:
                log("RESPONSE", "SKIPPED", flow.request.pretty_url, extra="no choices in response body")
                return

            assistant_reply = choices[0].get("message", {}).get("content", "")
            status = flow.response.status_code

            log("RESPONSE", "REPLY", assistant_reply, extra=f"http_status={status}")

            # Run Guardian on the upstream reply to catch any unsafe content
            try:
                if check_guardian(assistant_reply):
                    log("RESPONSE", "FLAGGED", assistant_reply,
                         extra="Guardian flagged upstream reply — redacting")

                    redacted_msg = "the response was redacted because it was considered unsafe"
                    body["choices"][0]["message"]["content"] = redacted_msg

                    flow.response.content = json.dumps(body).encode("utf-8")
                    flow.response.headers["Content-Length"] = str(len(flow.response.content))
            except Exception as e:
                ctx.log.warn(f"[GUARDIAN] Response check failed: {e}")

        except Exception as e:
            ctx.log.warn(f"[RESPONSE MONITOR] Could not parse response body: {e}")


addons = [LLMGateway()]