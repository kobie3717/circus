"""Task lifecycle state machine and validation."""

from circus.models import TaskState


# Valid state transitions
STATE_MACHINE = {
    TaskState.SUBMITTED: [TaskState.WORKING, TaskState.CANCELED],
    TaskState.WORKING: [TaskState.INPUT_REQUIRED, TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED],
    TaskState.INPUT_REQUIRED: [TaskState.WORKING, TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED],
    TaskState.COMPLETED: [],  # Terminal state
    TaskState.FAILED: [],     # Terminal state
    TaskState.CANCELED: [],   # Terminal state
}


def is_valid_transition(current_state: TaskState, new_state: TaskState) -> bool:
    """Check if state transition is valid."""
    return new_state in STATE_MACHINE.get(current_state, [])


def is_terminal_state(state: TaskState) -> bool:
    """Check if state is terminal (no further transitions)."""
    return len(STATE_MACHINE.get(state, [])) == 0
