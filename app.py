from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
import json
import logging
import os
import re
import time

import gradio as gr
import requests
from dotenv import load_dotenv
from openai import OpenAI
from pypdf import PdfReader


load_dotenv(override=True)

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
DEFAULT_MODEL = "gpt-4o-mini"
STARTER_QUESTIONS = [
    "Provide me a summary of your professional career",
    "What is your highest level of education",
    "What is most recent or current job",
]
STARTER_MESSAGE = (
    "You can start by clicking one of these questions:\n\n"
    "1. Provide me a summary of your professional career\n"
    "2. What is your highest level of education\n"
    "3. What is most recent or current job"
)
MAX_MESSAGE_CHARS = int(os.getenv("MAX_MESSAGE_CHARS", "2000"))
RATE_LIMIT_MESSAGES = int(os.getenv("RATE_LIMIT_MESSAGES", "12"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
TOOL_LIMIT_PER_SESSION = int(os.getenv("TOOL_LIMIT_PER_SESSION", "5"))
TOOL_LIMIT_WINDOW_SECONDS = int(os.getenv("TOOL_LIMIT_WINDOW_SECONDS", "3600"))
MAX_LINKS_PER_MESSAGE = int(os.getenv("MAX_LINKS_PER_MESSAGE", "4"))
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
URL_RE = re.compile(r"https?://", re.IGNORECASE)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def push(text):
    token = os.getenv("PUSHOVER_TOKEN")
    user = os.getenv("PUSHOVER_USER")

    if not token or not user:
        logger.warning("Pushover credentials are missing; notification skipped.")
        return False

    try:
        response = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": token,
                "user": user,
                "message": text,
            },
            timeout=10,
        )
        response.raise_for_status()
        return True
    except requests.RequestException:
        logger.exception("Unable to send Pushover notification.")
        return False


def write_jsonl(path, payload):
    LOG_DIR.mkdir(exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def record_user_details(email, name="Name not provided", notes="not provided"):
    notification_sent = push(
        f"Recording {name} with email {email} and notes {notes}"
    )
    write_jsonl(
        LOG_DIR / "user_details.jsonl",
        {
            "event": "user_details",
            "email": email,
            "name": name,
            "notes": notes,
            "notification_sent": notification_sent,
        },
    )
    return {"recorded": "ok", "notification_sent": notification_sent}


def record_unknown_question(question):
    notification_sent = push(f"Recording {question}")
    write_jsonl(
        LOG_DIR / "unknown_questions.jsonl",
        {
            "event": "unknown_question",
            "question": question,
            "notification_sent": notification_sent,
        },
    )
    return {"recorded": "ok", "notification_sent": notification_sent}


record_user_details_json = {
    "type": "function",
    "name": "record_user_details",
    "description": "Use this tool to record that a user is interested in being in touch and provided an email address",
    "parameters": {
        "type": "object",
        "properties": {
            "email": {
                "type": "string",
                "description": "The email address of this user",
            },
            "name": {
                "type": "string",
                "description": "The user's name, if they provided it",
            },
            "notes": {
                "type": "string",
                "description": "Any additional information about the conversation that's worth recording to give context",
            },
        },
        "required": ["email"],
        "additionalProperties": False,
    },
}

record_unknown_question_json = {
    "type": "function",
    "name": "record_unknown_question",
    "description": "Always use this tool to record any question that couldn't be answered as you didn't know the answer",
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question that couldn't be answered",
            },
        },
        "required": ["question"],
        "additionalProperties": False,
    },
}

tools = [record_user_details_json, record_unknown_question_json]

AVAILABLE_TOOLS = {
    "record_user_details": record_user_details,
    "record_unknown_question": record_unknown_question,
}


class Me:

    def __init__(self):
        self.openai = OpenAI()
        self.model = os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
        self.name = "Abdul S Anwar"
        self.resume = self.read_pdf(BASE_DIR / "me" / "abdul-resume.pdf")
        self.summary = self.read_text(BASE_DIR / "me" / "summary.txt")
        self.sessions_seen = set()
        self.message_timestamps = defaultdict(deque)
        self.tool_timestamps = defaultdict(deque)

        if not os.getenv("OPENAI_API_KEY"):
            logger.warning("OPENAI_API_KEY is not set.")

    def read_pdf(self, path):
        if not path.exists():
            logger.warning("Resume PDF not found at %s", path)
            return "Resume PDF was not found."

        try:
            reader = PdfReader(str(path))
        except Exception:
            logger.exception("Unable to read resume PDF at %s", path)
            return "Resume PDF could not be read."

        pages = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
        return "\n".join(pages) or "Resume PDF did not contain extractable text."

    def read_text(self, path):
        if not path.exists():
            logger.warning("Summary file not found at %s", path)
            return "Summary file was not found."

        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            logger.exception("Unable to read summary file at %s", path)
            return "Summary file could not be read."

    def session_id(self, request):
        if request and getattr(request, "session_hash", None):
            return request.session_hash
        return "anonymous"

    def identify_session(self, session_id, request):
        if session_id in self.sessions_seen:
            return

        client = getattr(request, "client", None)
        client_host = getattr(client, "host", None)
        self.sessions_seen.add(session_id)
        write_jsonl(
            LOG_DIR / "sessions.jsonl",
            {
                "event": "session_started",
                "session_id": session_id,
                "client_host": client_host,
            },
        )

    def prune_timestamps(self, timestamps, window_seconds):
        cutoff = time.time() - window_seconds
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()

    def check_message_allowed(self, session_id, message):
        cleaned = (message or "").strip()
        if not cleaned:
            return False, "Please enter a message."

        if len(cleaned) > MAX_MESSAGE_CHARS:
            return False, "That message is a bit too long. Please shorten it and try again."

        if len(URL_RE.findall(cleaned)) > MAX_LINKS_PER_MESSAGE:
            return False, "Too many links were included. Please send a shorter message."

        timestamps = self.message_timestamps[session_id]
        self.prune_timestamps(timestamps, RATE_LIMIT_WINDOW_SECONDS)
        if len(timestamps) >= RATE_LIMIT_MESSAGES:
            return False, "You're sending messages very quickly. Please wait a moment and try again."

        timestamps.append(time.time())
        return True, None

    def log_blocked_request(self, session_id, reason, message):
        write_jsonl(
            LOG_DIR / "blocked_requests.jsonl",
            {
                "event": "blocked_request",
                "session_id": session_id,
                "reason": reason,
                "message": message,
            },
        )

    def log_conversation(self, session_id, message, answer):
        write_jsonl(
            LOG_DIR / "conversations.jsonl",
            {
                "event": "conversation",
                "session_id": session_id,
                "user_message": message,
                "assistant_message": answer,
            },
        )

    def check_tool_allowed(self, session_id, tool_name, arguments):
        tool = AVAILABLE_TOOLS.get(tool_name)
        if not tool:
            logger.warning("Rejected unknown tool call: %s", tool_name)
            return False, f"Unknown tool: {tool_name}"

        timestamps = self.tool_timestamps[session_id]
        self.prune_timestamps(timestamps, TOOL_LIMIT_WINDOW_SECONDS)
        if len(timestamps) >= TOOL_LIMIT_PER_SESSION:
            return False, "Tool usage limit reached for this session."

        if tool_name == "record_user_details":
            email = (arguments.get("email") or "").strip()
            if not EMAIL_RE.match(email):
                return False, "A valid email address is required to record user details."

        if tool_name == "record_unknown_question":
            question = (arguments.get("question") or "").strip()
            if len(question) < 3:
                return False, "A question is required before recording an unknown question."
            if len(question) > 500:
                return False, "Question is too long to record."

        timestamps.append(time.time())
        return True, None

    def call_tool(self, session_id, tool_name, arguments):
        allowed, reason = self.check_tool_allowed(session_id, tool_name, arguments)
        if not allowed:
            logger.warning("Rejected tool call %s: %s", tool_name, reason)
            return {"error": reason}

        tool = AVAILABLE_TOOLS[tool_name]

        try:
            return tool(**arguments)
        except TypeError as exc:
            logger.exception("Invalid arguments for tool %s", tool_name)
            return {"error": f"Invalid arguments for {tool_name}: {exc}"}
        except Exception as exc:
            logger.exception("Tool %s failed", tool_name)
            return {"error": f"Tool {tool_name} failed: {exc}"}

    def clean_history(self, history):
        messages = []

        for item in history or []:
            if not isinstance(item, dict):
                continue

            role = item.get("role")
            content = item.get("content")

            if role not in {"user", "assistant"}:
                continue

            if role == "assistant" and content == STARTER_MESSAGE:
                continue

            if not isinstance(content, str) or not content.strip():
                continue

            messages.append({"role": role, "content": content})

        return messages

    def handle_tool_calls(self, response, session_id):
        tool_call_messages = []
        tool_result_messages = []

        for item in response.output:
            if item.type != "function_call":
                continue

            try:
                arguments = json.loads(item.arguments or "{}")
            except json.JSONDecodeError:
                logger.exception("Could not parse arguments for tool %s", item.name)
                arguments = {}

            logger.info("Tool called: %s", item.name)
            result = self.call_tool(session_id, item.name, arguments)
            tool_call_messages.append(
                {
                    "type": "function_call",
                    "call_id": item.call_id,
                    "name": item.name,
                    "arguments": item.arguments,
                }
            )
            tool_result_messages.append(
                {
                    "type": "function_call_output",
                    "call_id": item.call_id,
                    "output": json.dumps(result),
                }
            )

        return tool_call_messages, tool_result_messages

    def system_prompt(self):
        return f"""
You are acting as {self.name}.

## Role
You are answering questions on {self.name}'s website, particularly questions related to {self.name}'s career, background, skills, and experience.
Represent {self.name} as faithfully as possible.
Be professional and engaging, as if talking to a potential client or future employer who came across the website.

## Tool Use
If you don't know the answer to any question, use the record_unknown_question tool to record the question that you couldn't answer, even if it's trivial or unrelated to career.
If the user is engaging in discussion, try to steer them towards getting in touch via email; ask for their email and record it using the record_user_details tool.

## Summary
{self.summary}

## LinkedIn Profile
{self.resume}

With this context, chat with the user while staying in character as {self.name}.
""".strip()

    def chat(self, message, history, request: gr.Request = None):
        session_id = self.session_id(request)
        self.identify_session(session_id, request)

        allowed, reason = self.check_message_allowed(session_id, message)
        if not allowed:
            self.log_blocked_request(session_id, reason, message)
            return reason

        input_messages = [
            *self.clean_history(history),
            {"role": "user", "content": message},
        ]

        for _ in range(5):
            response = self.openai.responses.create(
                model=self.model,
                instructions=self.system_prompt(),
                input=input_messages,
                tools=tools,
            )

            tool_calls, tool_results = self.handle_tool_calls(response, session_id)
            if not tool_results:
                answer = response.output_text
                self.log_conversation(session_id, message, answer)
                return answer

            input_messages.extend(tool_calls)
            input_messages.extend(tool_results)

        answer = "I am sorry, but I could not complete the tool workflow for that request."
        self.log_conversation(session_id, message, answer)
        return answer


def build_app():
    me = Me()

    def respond(message, history, request: gr.Request = None):
        history = history or []
        answer = me.chat(message, history, request)
        history = [
            *history,
            {"role": "user", "content": message},
            {"role": "assistant", "content": answer},
        ]
        return "", history

    def ask_starter(question, history, request: gr.Request = None):
        history = history or []
        answer = me.chat(question, history, request)
        return [
            *history,
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer},
        ]

    def make_starter_handler(question):
        def handler(history, request: gr.Request = None):
            return ask_starter(question, history, request)

        return handler

    with gr.Blocks(title=f"Chat with {me.name}") as demo:
        gr.Markdown("## Chat with Abdul S Anwar")
        gr.Markdown("Start with one of these questions:")

        starter_buttons = []
        with gr.Row():
            for question in STARTER_QUESTIONS:
                starter = gr.Button(question, variant="secondary")
                starter_buttons.append((starter, question))

        chatbot = gr.Chatbot(
            type="messages",
            value=[{"role": "assistant", "content": STARTER_MESSAGE}],
            placeholder="Choose a starter question above, or ask your own question below.",
        )

        for starter, question in starter_buttons:
            starter.click(
                make_starter_handler(question),
                inputs=[chatbot],
                outputs=[chatbot],
            )

        message = gr.Textbox(
            placeholder="Ask a question...",
            show_label=False,
        )
        gr.Examples(
            examples=STARTER_QUESTIONS,
            inputs=message,
            label="Starter questions",
        )
        message.submit(
            respond,
            inputs=[message, chatbot],
            outputs=[message, chatbot],
        )

    return demo


if __name__ == "__main__":
    build_app().launch()
