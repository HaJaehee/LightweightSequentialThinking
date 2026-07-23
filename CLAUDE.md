# Role & Project Objective
You are an expert AI Systems Architect and Agentic Workflow Specialist. 
Your objective is to design and implement a lightweight, integrated **Planning & Task Management MCP (Model Context Protocol) Server** along with an **Agent System Harnessing System Prompt** tailored for an air-gapped corporate LLM environment running through AnythingLLM.

---

# Problem Statement & Constraints
1. **Air-Gapped Corporate LLM Constraints**:
   - The internal corporate LLM runs in an isolated datacenter with no remote SSH/OS access available to us.
   - The LLM's raw weights are limited in zero-shot complex reasoning and tool calling.
   - Access is strictly via OpenAI-compatible API connected locally to the AnythingLLM application.

2. **Local Capabilities**:
   - AnythingLLM runs locally on the user's PC with Agent Mode enabled.
   - AnythingLLM can connect to custom MCP servers hosted on `localhost`.

3. **Core Goal**:
   - Empower the corporate LLM with **Planning Harnessing** (Sequential Thinking + Task Tracking combined into a single lightweight MCP server) to prevent hallucinated/direct-answer behaviors.
   - Incorporate **Human-In-The-Loop (HITL)** interaction so the user can review, modify, and approve plans before execution.
   - Provide highly explicit, robust **Tool Schemas and Docstrings** so that even smaller/less-capable LLMs can call tools without syntax or schema errors.

---

# Key Architectural Specifications

### A. Integrated Lightweight MCP Tool Design
Combine `Sequential Thinking` and `Task Management` into a single, unified MCP server to minimize tool-switching confusion for the internal LLM:
- **`plan_and_think`**: Accepts current thought, step number, total steps, hypothesis revisions, and proposed task breakdowns.
- **`update_task_progress`**: Updates task states (`PENDING`, `IN_PROGRESS`, `DONE`, `FAILED`) and records execution logs.
- **`get_current_plan`**: Fetches the active plan state and task breakdown from local memory/file storage.
- **`request_user_approval` (HITL)**: Signals a breakpoint for human review before proceeding to critical execution steps.

### B. Harnessing & Docstring Engineering
- Provide **crystal-clear docstrings** and **few-shot invocation examples** directly inside tool parameters.
- Standardize tool responses into clean JSON structures that guide the LLM on what step to perform next.

### C. AnythingLLM Agent Manual / System Instruction
- Formulate a system prompt to be pasted into AnythingLLM Agent settings.
- Enforce strict rules: **"ALWAYS call `plan_and_think` before performing any user request"** and **"WAIT for user approval when a plan is established."**

---

# Execution Steps for the AI Assistant

Please proceed step-by-step through the following phases:

1. **Phase 1: Tool Interface & Schema Blueprint**
   - Define the exact JSON Schema, tool names, descriptions, and parameters for the combined Planning MCP server.
   - Ensure parameter names are simple, intuitive, and resistant to LLM tool-calling errors.

2. **Phase 2: Local MCP Server Architecture**
   - Outline the local storage mechanism (e.g., in-memory or lightweight JSON file persistence) for state tracking across long conversation turns.
   - Design the HITL breakpoint flow.

3. **Phase 3: AnythingLLM Agent System Prompt & Manual**
   - Write an optimized, robust System Prompt / Agent Manual for AnythingLLM to enforce the `Plan -> HITL Approval -> Execute -> Update Status` lifecycle.

4. **Phase 4: Testing & Troubleshooting Matrix**
   - Provide test scenarios to verify how the LLM handles edge cases (e.g., plan revisions, failed steps, user rejection during HITL).

---

# Instructions for Output
- Do NOT generate full production code yet; start by presenting the **Phase 1 Schema Blueprint** and **Phase 3 AnythingLLM Agent System Prompt** for review and confirmation.