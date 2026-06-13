"""
Canonical eval task suite for Circus capability attestation.
Each task has: id, capability_tag, title, description, input, rubric (what a good answer contains), min_score
"""

EVAL_TASKS = [
    # SQL optimization (3 tasks)
    {
        "id": "eval-sql-001",
        "capability_tag": "sql-optimization",
        "title": "Optimize slow query",
        "description": "Rewrite the given SQL query to improve performance",
        "input": "SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id WHERE c.country = 'ZA' AND o.created_at > '2024-01-01'",
        "rubric": ["uses index hints or proper column selection", "avoids SELECT *", "considers index on created_at", "filters pushed down"],
        "min_score": 0.7,
    },
    {
        "id": "eval-sql-002",
        "capability_tag": "sql-optimization",
        "title": "Identify N+1 query",
        "description": "Identify the N+1 problem in this ORM usage and provide fix",
        "input": "for user in User.query.all():\n    print(user.orders.count())",
        "rubric": ["identifies N+1 pattern", "suggests eager loading or JOIN", "provides working code fix"],
        "min_score": 0.7,
    },
    {
        "id": "eval-sql-003",
        "capability_tag": "sql-optimization",
        "title": "Index strategy",
        "description": "Given a table schema and query pattern, recommend indexes",
        "input": "Table: transactions(id, user_id, amount, status, created_at, merchant_id). Query pattern: filter by user_id + status, order by created_at",
        "rubric": ["composite index on user_id+status", "considers created_at ordering", "avoids over-indexing"],
        "min_score": 0.7,
    },
    # Text summarization (3 tasks)
    {
        "id": "eval-text-001",
        "capability_tag": "text-summarization",
        "title": "Summarize technical doc",
        "description": "Summarize the key points in under 100 words",
        "input": "FastAPI is a modern, fast (high-performance), web framework for building APIs with Python 3.7+ based on standard Python type hints. The key features are: Fast: Very high performance, on par with NodeJS and Go (thanks to Starlette and Pydantic). One of the fastest Python frameworks available. Fast to code: Increase the speed to develop features by about 200% to 300%. Fewer bugs: Reduce about 40% of human (developer) induced errors. Intuitive: Great editor support. Completion everywhere. Less time debugging. Easy: Designed to be easy to use and learn. Less time reading docs. Short: Minimize code duplication.",
        "rubric": ["under 100 words", "captures main points: performance, speed of development, fewer bugs", "clear and accurate"],
        "min_score": 0.7,
    },
    {
        "id": "eval-text-002",
        "capability_tag": "text-summarization",
        "title": "Extract action items",
        "description": "Extract all action items from this meeting transcript",
        "input": "John: We need to fix the login bug before Friday. Sarah: I will handle that. Also, someone should update the docs for the new API. Tom: I can do the docs by Wednesday. John: Great, and let's also make sure the staging environment is updated. Sarah: I'll coordinate with DevOps on that.",
        "rubric": ["identifies 3 action items", "assigns owners correctly: Sarah=login bug+staging, Tom=docs", "includes deadlines where mentioned"],
        "min_score": 0.7,
    },
    {
        "id": "eval-text-003",
        "capability_tag": "text-summarization",
        "title": "Classify sentiment",
        "description": "Classify the sentiment of each review and explain why",
        "input": "Review 1: 'Amazing product, changed my life!' Review 2: 'Arrived broken, terrible experience.' Review 3: 'It works as described, nothing special.'",
        "rubric": ["correctly classifies: positive/negative/neutral", "provides brief justification for each", "handles all 3 reviews"],
        "min_score": 0.7,
    },
    # Code review (3 tasks)
    {
        "id": "eval-code-001",
        "capability_tag": "code-review",
        "title": "Find security vulnerability",
        "description": "Identify the security issue in this Python code",
        "input": "def get_user(user_id):\n    query = f'SELECT * FROM users WHERE id = {user_id}'\n    return db.execute(query)",
        "rubric": ["identifies SQL injection", "explains risk", "provides parameterized query fix"],
        "min_score": 0.7,
    },
    {
        "id": "eval-code-002",
        "capability_tag": "code-review",
        "title": "Spot race condition",
        "description": "Identify the concurrency issue and suggest a fix",
        "input": "counter = 0\ndef increment():\n    global counter\n    temp = counter\n    time.sleep(0.001)\n    counter = temp + 1\n\nthreads = [threading.Thread(target=increment) for _ in range(100)]\n[t.start() for t in threads]",
        "rubric": ["identifies race condition on counter", "explains lost update problem", "suggests lock or atomic operation"],
        "min_score": 0.7,
    },
    {
        "id": "eval-code-003",
        "capability_tag": "code-review",
        "title": "Identify memory leak",
        "description": "Find the memory leak in this JavaScript code",
        "input": "const cache = {};\nfunction processRequest(req) {\n    const key = req.id;\n    cache[key] = { data: req.data, timestamp: Date.now() };\n    return cache[key];\n}",
        "rubric": ["identifies unbounded cache growth", "notes missing eviction policy", "suggests TTL or size limit fix"],
        "min_score": 0.7,
    },
    # Data extraction (2 tasks)
    {
        "id": "eval-extract-001",
        "capability_tag": "data-extraction",
        "title": "Extract structured data from text",
        "description": "Extract all entities as JSON",
        "input": "Invoice #INV-2024-001 from Acme Corp (acme@corp.com) to Beta Ltd. Amount: R15,000 due 2024-03-15. Items: 5x Widget A @ R1,500 = R7,500, 5x Widget B @ R1,500 = R7,500.",
        "rubric": ["extracts invoice_number, vendor, client, amount, due_date, line_items", "correct values", "valid JSON output"],
        "min_score": 0.7,
    },
    {
        "id": "eval-extract-002",
        "capability_tag": "data-extraction",
        "title": "Parse auction lot from WhatsApp message",
        "description": "Extract auction lot details as structured JSON",
        "input": "LOT 42 - 2019 Toyota Hilux 2.8 GD6 4x4 Double Cab. KM: 87,432. Color: White. Condition: Good. Reserve: R285,000. Opening bid: R220,000. Closes: 15 June 2024 18:00 SAST.",
        "rubric": ["extracts lot_number, vehicle details, mileage, reserve_price, opening_bid, close_time", "correct types (numbers as numbers)", "valid JSON"],
        "min_score": 0.7,
    },
    # Reasoning (2 tasks)
    {
        "id": "eval-reason-001",
        "capability_tag": "logical-reasoning",
        "title": "Detect logical fallacy",
        "description": "Identify the logical fallacy and explain it",
        "input": "Everyone is investing in crypto right now. You should too, or you will miss out.",
        "rubric": ["identifies bandwagon fallacy and/or appeal to fear", "explains why it is fallacious", "concise explanation"],
        "min_score": 0.7,
    },
    {
        "id": "eval-reason-002",
        "capability_tag": "logical-reasoning",
        "title": "Solve constraint problem",
        "description": "Find the answer given the constraints",
        "input": "Alice, Bob, and Carol each have a different pet (cat, dog, fish). Alice does not have a cat. Bob does not have a dog. Carol has the fish. Who has what pet?",
        "rubric": ["correctly deduces: Carol=fish, Alice=dog, Bob=cat", "shows reasoning steps"],
        "min_score": 0.7,
    },
    # Translation/language (2 tasks)
    {
        "id": "eval-lang-001",
        "capability_tag": "translation",
        "title": "Translate with cultural context",
        "description": "Translate this Afrikaans phrase to English and explain any cultural nuance",
        "input": "Ag nee wat, dis mos nou te veel gevra!",
        "rubric": ["accurate translation", "captures informal/exasperated tone", "notes South African context if relevant"],
        "min_score": 0.6,
    },
    {
        "id": "eval-lang-002",
        "capability_tag": "translation",
        "title": "Detect language and translate",
        "description": "Identify the language and translate to English",
        "input": "Bonjour, je voudrais confirmer ma réservation pour demain soir.",
        "rubric": ["correctly identifies French", "accurate translation about reservation confirmation", "natural English output"],
        "min_score": 0.7,
    },
    # Planning (2 tasks)
    {
        "id": "eval-plan-001",
        "capability_tag": "task-planning",
        "title": "Break down complex task",
        "description": "Break this goal into ordered, verifiable subtasks",
        "input": "Goal: Deploy a new API endpoint that reads from a database and returns JSON, with proper error handling and tests.",
        "rubric": ["identifies 5+ subtasks", "logical order (schema before code, tests before deploy)", "each subtask has clear done condition", "includes error handling step"],
        "min_score": 0.7,
    },
    {
        "id": "eval-plan-002",
        "capability_tag": "task-planning",
        "title": "Identify task dependencies",
        "description": "Given these tasks, identify which must happen before others",
        "input": "Tasks: A=write tests, B=deploy to prod, C=write code, D=code review, E=set up database schema",
        "rubric": ["correct dependency graph: E->C->A->D->B", "explains each dependency", "identifies that B must be last"],
        "min_score": 0.7,
    },
    # WhatsApp/auction domain (3 tasks — domain-specific, relevant to WhatsAuction)
    {
        "id": "eval-auction-001",
        "capability_tag": "auction-domain",
        "title": "Validate bid",
        "description": "Given auction state, determine if this bid is valid and why",
        "input": "Current lot: LOT-5. Current high bid: R45,000. Increment: R1,000. Proposed bid: R45,500. Bidder trust tier: Newcomer.",
        "rubric": ["bid R45,500 is valid (meets increment)", "notes Newcomer tier may have restrictions", "explains bid validation logic"],
        "min_score": 0.7,
    },
    {
        "id": "eval-auction-002",
        "capability_tag": "auction-domain",
        "title": "Draft WhatsApp auction update",
        "description": "Write a WhatsApp message announcing a new high bid",
        "input": "Lot: 2019 BMW 3 Series. Previous high: R180,000. New high: R185,000. Bidder: Anonymous. Time remaining: 10 minutes.",
        "rubric": ["clear and urgent tone", "all key info included (lot, amount, time)", "WhatsApp-appropriate (no markdown tables, concise)", "encourages other bids"],
        "min_score": 0.7,
    },
    {
        "id": "eval-auction-003",
        "capability_tag": "auction-domain",
        "title": "Detect shill bidding pattern",
        "description": "Given this bid history, identify if shill bidding is occurring and why",
        "input": "Bids on LOT-7: 09:00 Bidder-A R100,000 | 09:01 Bidder-B R101,000 | 09:01 Bidder-A R102,000 | 09:01 Bidder-B R103,000 | 09:02 Bidder-A R104,000. Bidder-A and Bidder-B registered from same IP.",
        "rubric": ["identifies suspicious pattern (same-second bids, same IP)", "labels as potential shill bidding", "recommends action (flag, investigate, halt)"],
        "min_score": 0.7,
    },
]


def get_eval_task(task_id: str) -> dict | None:
    """Get a specific eval task by ID."""
    return next((t for t in EVAL_TASKS if t["id"] == task_id), None)


def get_evals_by_capability(capability_tag: str) -> list[dict]:
    """Get all eval tasks for a specific capability tag."""
    return [t for t in EVAL_TASKS if t["capability_tag"] == capability_tag]


def get_all_capability_tags() -> list[str]:
    """Get sorted list of all capability tags."""
    return sorted(set(t["capability_tag"] for t in EVAL_TASKS))
