import logging
import asyncio
from typing import Dict, Optional, List, Any, AsyncGenerator
from pydantic import BaseModel
from ..agent.agent import Agent
from ..task.task import Task
from ..main import display_error, client
import csv
import os

class LoopItems(BaseModel):
    items: List[Any]

class Process:
    DEFAULT_RETRY_LIMIT = 3  # Predefined retry limit in a common place

    def __init__(self, tasks: Dict[str, Task], agents: List[Agent], manager_llm: Optional[str] = None, verbose: bool = False, max_iter: int = 10):
        logging.debug(f"=== Initializing Process ===")
        logging.debug(f"Number of tasks: {len(tasks)}")
        logging.debug(f"Number of agents: {len(agents)}")
        logging.debug(f"Manager LLM: {manager_llm}")
        logging.debug(f"Verbose mode: {verbose}")
        logging.debug(f"Max iterations: {max_iter}")

        self.tasks = tasks
        self.agents = agents
        self.manager_llm = manager_llm
        self.verbose = verbose
        self.max_iter = max_iter
        self.task_retry_counter: Dict[str, int] = {} # Initialize retry counter

    def _find_next_not_started_task(self) -> Optional[Task]:
        """Fallback mechanism to find the next 'not started' task."""
        fallback_attempts = 0
        temp_current_task = None
        while fallback_attempts < Process.DEFAULT_RETRY_LIMIT and not temp_current_task:
            fallback_attempts += 1
            logging.debug(f"Fallback attempt {fallback_attempts}: Trying to find next 'not started' task.")
            for task_candidate in self.tasks.values():
                if task_candidate.status == "not started":
                    if self.task_retry_counter.get(task_candidate.id, 0) < Process.DEFAULT_RETRY_LIMIT:
                        self.task_retry_counter[task_candidate.id] = self.task_retry_counter.get(task_candidate.id, 0) + 1
                        temp_current_task = task_candidate
                        logging.debug(f"Fallback attempt {fallback_attempts}: Found 'not started' task: {temp_current_task.name}, retry count: {self.task_retry_counter[temp_current_task.id]}")
                        return temp_current_task # Return the found task immediately
                    else:
                        logging.debug(f"Max retries reached for task {task_candidate.name} in fallback mode, marking as failed.")
                        task_candidate.status = "failed"
            if not temp_current_task:
                logging.debug(f"Fallback attempt {fallback_attempts}: No 'not started' task found within retry limit.")
        return None # Return None if no task found after all attempts


    async def aworkflow(self) -> AsyncGenerator[str, None]:
        """Async version of workflow method"""
        logging.debug("=== Starting Async Workflow ===")
        current_iter = 0  # Track how many times we've looped
        # Build workflow relationships first
        logging.debug("Building workflow relationships...")
        for task in self.tasks.values():
            if task.next_tasks:
                for next_task_name in task.next_tasks:
                    next_task = next((t for t in self.tasks.values() if t.name == next_task_name), None)
                    if next_task:
                        next_task.previous_tasks.append(task.name)
                        logging.debug(f"Added {task.name} as previous task for {next_task_name}")

        # Find start task
        logging.debug("Finding start task...")
        start_task = None
        for task_id, task in self.tasks.items():
            if task.is_start:
                start_task = task
                logging.debug(f"Found marked start task: {task.name} (id: {task_id})")
                break

        if not start_task:
            start_task = list(self.tasks.values())[0]
            logging.debug(f"No start task marked, using first task: {start_task.name}")

        current_task = start_task
        visited_tasks = set()
        loop_data = {}  # Store loop-specific data

        # TODO: start task with loop feature is not available in aworkflow method

        while current_task:
            current_iter += 1
            if current_iter > self.max_iter:
                logging.info(f"Max iteration limit {self.max_iter} reached, ending workflow.")
                break

            # Add task summary at start of each cycle
            logging.debug(f"""
=== Workflow Cycle {current_iter} Summary ===
Total tasks: {len(self.tasks)}
Outstanding tasks: {sum(1 for t in self.tasks.values() if t.status != "completed")}
Completed tasks: {sum(1 for t in self.tasks.values() if t.status == "completed")}
Tasks by status:
- Not started: {sum(1 for t in self.tasks.values() if t.status == "not started")}
- In progress: {sum(1 for t in self.tasks.values() if t.status == "in_progress")}
- Completed: {sum(1 for t in self.tasks.values() if t.status == "completed")}
Tasks by type:
- Loop tasks: {sum(1 for t in self.tasks.values() if t.task_type == "loop")}
- Decision tasks: {sum(1 for t in self.tasks.values() if t.task_type == "decision")}
- Regular tasks: {sum(1 for t in self.tasks.values() if t.task_type not in ["loop", "decision"])}
            """)

            task_id = current_task.id
            logging.debug(f"""
=== Task Execution Details ===
Current task: {current_task.name}
Type: {current_task.task_type}
Status: {current_task.status}
Previous tasks: {current_task.previous_tasks}
Next tasks: {current_task.next_tasks}
Context tasks: {[t.name for t in current_task.context] if current_task.context else []}
Description length: {len(current_task.description)}
            """)

            # Add context from previous tasks to description
            if current_task.previous_tasks or current_task.context:
                context = "\nInput data from previous tasks:"

                # Add data from previous tasks in workflow
                for prev_name in current_task.previous_tasks:
                    prev_task = next((t for t in self.tasks.values() if t.name == prev_name), None)
                    if prev_task and prev_task.result:
                        # Handle loop data
                        if current_task.task_type == "loop":
                            context += f"\n{prev_name}: {prev_task.result.raw}"
                        else:
                            context += f"\n{prev_name}: {prev_task.result.raw}"

                # Add data from context tasks
                if current_task.context:
                    for ctx_task in current_task.context:
                        if ctx_task.result and ctx_task.name != current_task.name:
                            context += f"\n{ctx_task.name}: {ctx_task.result.raw}"

                # Update task description with context
                current_task.description = current_task.description + context

            # Skip execution for loop tasks, only process their subtasks
            if current_task.task_type == "loop":
                logging.debug(f"""
=== Loop Task Details ===
Name: {current_task.name}
ID: {current_task.id}
Status: {current_task.status}
Next tasks: {current_task.next_tasks}
Condition: {current_task.condition}
Subtasks created: {getattr(current_task, '_subtasks_created', False)}
Input file: {getattr(current_task, 'input_file', None)}
                """)

                # Check if subtasks are created and completed
                if getattr(current_task, "_subtasks_created", False):
                    subtasks = [
                        t for t in self.tasks.values()
                        if t.name.startswith(current_task.name + "_")
                    ]
                    logging.debug(f"""
=== Subtask Status Check ===
Total subtasks: {len(subtasks)}
Completed: {sum(1 for st in subtasks if st.status == "completed")}
Pending: {sum(1 for st in subtasks if st.status != "completed")}
                    """)

                    # Log detailed subtask info
                    for st in subtasks:
                        logging.debug(f"""
Subtask: {st.name}
- Status: {st.status}
- Next tasks: {st.next_tasks}
- Condition: {st.condition}
                        """)

                    if subtasks and all(st.status == "completed" for st in subtasks):
                        logging.debug(f"=== All {len(subtasks)} subtasks completed for {current_task.name} ===")

                        # Mark loop task completed and move to next task
                        current_task.status = "completed"
                        logging.debug(f"Loop {current_task.name} marked as completed")

                        # Move to next task if available
                        if current_task.next_tasks:
                            next_task_name = current_task.next_tasks[0]
                            logging.debug(f"Attempting transition to next task: {next_task_name}")
                            next_task = next((t for t in self.tasks.values() if t.name == next_task_name), None)
                            if next_task:
                                logging.debug(f"=== Transitioning: {current_task.name} -> {next_task.name} ===")
                                logging.debug(f"Next task status: {next_task.status}")
                                logging.debug(f"Next task condition: {next_task.condition}")
                            current_task = next_task
                        else:
                            logging.debug(f"=== No next tasks for {current_task.name}, checking fallback ===")
                            current_task = self._find_next_not_started_task() # Fallback here only after loop completion
                else:
                    logging.debug(f"No subtasks created yet for {current_task.name}")
                    # Create subtasks if needed
                    if current_task.input_file:
                        self._create_loop_subtasks(current_task)
                        current_task._subtasks_created = True
                        logging.debug(f"Created subtasks from {current_task.input_file}")
                    else:
                        # No input file, mark as done
                        current_task.status = "completed"
                        logging.debug(f"No input file, marking {current_task.name} as completed")
                        if current_task.next_tasks:
                            next_task_name = current_task.next_tasks[0]
                            next_task = next((t for t in self.tasks.values() if t.name == next_task_name), None)
                            current_task = next_task
                        else:
                            current_task = None
            else:
                # Execute non-loop task
                logging.debug(f"=== Executing non-loop task: {current_task.name} (id: {task_id}) ===")
                logging.debug(f"Task status: {current_task.status}")
                logging.debug(f"Task next_tasks: {current_task.next_tasks}")
                yield task_id
                visited_tasks.add(task_id)

            # Reset completed task to "not started" so it can run again
            if self.tasks[task_id].status == "completed":
                # Never reset loop tasks, decision tasks, or their subtasks
                subtask_name = self.tasks[task_id].name
                logging.debug(f"=== Checking reset for completed task: {subtask_name} ===")
                logging.debug(f"Task type: {self.tasks[task_id].task_type}")
                logging.debug(f"Task status before reset check: {self.tasks[task_id].status}")

                if (self.tasks[task_id].task_type not in ["loop", "decision"] and
                    not any(t.task_type == "loop" and subtask_name.startswith(t.name + "_")
                           for t in self.tasks.values())):
                    logging.debug(f"=== Resetting non-loop, non-decision task {subtask_name} to 'not started' ===")
                    self.tasks[task_id].status = "not started"
                    logging.debug(f"Task status after reset: {self.tasks[task_id].status}")
                else:
                    logging.debug(f"=== Skipping reset for loop/decision/subtask: {subtask_name} ===")
                    logging.debug(f"Keeping status as: {self.tasks[task_id].status}")

            # Handle loop progression
            if current_task.task_type == "loop":
                loop_key = f"loop_{current_task.name}"
                if loop_key in loop_data:
                    loop_info = loop_data[loop_key]
                    loop_info["index"] += 1
                    has_more = loop_info["remaining"] > 0

                    # Update result to trigger correct condition
                    if current_task.result:
                        result = current_task.result.raw
                        if has_more:
                            result += "\nmore"
                        else:
                            result += "\ndone"
                        current_task.result.raw = result

            # Determine next task based on result
            next_task = None
            if current_task and current_task.result:
                if current_task.task_type in ["decision", "loop"]:
                    # MINIMAL CHANGE: use pydantic decision if present
                    decision_str = current_task.result.raw.lower()
                    if current_task.result.pydantic and hasattr(current_task.result.pydantic, "decision"):
                        decision_str = current_task.result.pydantic.decision.lower()

                    # Check conditions
                    for condition, tasks in current_task.condition.items():
                        if condition.lower() == decision_str:
                            # Handle both list and direct string values
                            task_value = tasks[0] if isinstance(tasks, list) else tasks
                            if not task_value or task_value == "exit":
                                logging.info("Workflow exit condition met, ending workflow")
                                current_task = None
                                break
                            next_task_name = task_value
                            next_task = next((t for t in self.tasks.values() if t.name == next_task_name), None)
                            # For loops, allow revisiting the same task
                            if next_task and next_task.id == current_task.id:
                                visited_tasks.discard(current_task.id)
                            break

            if not next_task and current_task and current_task.next_tasks:
                next_task_name = current_task.next_tasks[0]
                next_task = next((t for t in self.tasks.values() if t.name == next_task_name), None)

            current_task = next_task
            if not current_task:
                current_task = self._find_next_not_started_task() # General fallback if no next task in workflow


            if not current_task:
                # Add final workflow summary
                logging.debug(f"""
=== Final Workflow Summary ===
Total tasks processed: {len(self.tasks)}
Final status:
- Completed tasks: {sum(1 for t in self.tasks.values() if t.status == "completed")}
- Outstanding tasks: {sum(1 for t in self.tasks.values() if t.status != "completed")}
Tasks by status:
- Not started: {sum(1 for t in self.tasks.values() if t.status == "not started")}
- In progress: {sum(1 for t in self.tasks.values() if t.status == "in_progress")}
- Completed: {sum(1 for t in self.tasks.values() if t.status == "completed")}
- Failed: {sum(1 for t in self.tasks.values() if t.status == "failed")}
Tasks by type:
- Loop tasks: {sum(1 for t in self.tasks.values() if t.task_type == "loop")}
- Decision tasks: {sum(1 for t in self.tasks.values() if t.task_type == "decision")}
- Regular tasks: {sum(1 for t in self.tasks.values() if t.task_type not in ["loop", "decision"])}
Total iterations: {current_iter}
                """)

                logging.info("Workflow execution completed")
                break

            # Add completion logging
            logging.debug(f"""
=== Task Completion ===
Task: {current_task.name}
Final status: {current_task.status}
Next task: {next_task.name if next_task else None}
Iteration: {current_iter}/{self.max_iter}
            """)

    async def asequential(self) -> AsyncGenerator[str, None]:
        """Async version of sequential method"""
        for task_id in self.tasks:
            if self.tasks[task_id].status != "completed":
                yield task_id

    async def ahierarchical(self) -> AsyncGenerator[str, None]:
        """Async version of hierarchical method"""
        logging.debug(f"Starting hierarchical task execution with {len(self.tasks)} tasks")
        manager_agent = Agent(
            name="Manager",
            role="Project manager",
            goal="Manage the entire flow of tasks and delegate them to the right agent",
            backstory="Expert project manager to coordinate tasks among agents",
            llm=self.manager_llm,
            verbose=self.verbose,
            markdown=True,
            self_reflect=False
        )

        class ManagerInstructions(BaseModel):
            task_id: int
            agent_name: str
            action: str

        manager_task = Task(
            name="manager_task",
            description="Decide the order of tasks and which agent executes them",
            expected_output="All tasks completed successfully",
            agent=manager_agent
        )
        manager_task_id = yield manager_task
        logging.info(f"Created manager task with ID {manager_task_id}")

        completed_count = 0
        total_tasks = len(self.tasks) - 1
        logging.info(f"Need to complete {total_tasks} tasks (excluding manager task)")

        while completed_count < total_tasks:
            tasks_summary = []
            for tid, tk in self.tasks.items():
                if tk.name == "manager_task":
                    continue
                task_info = {
                    "task_id": tid,
                    "name": tk.name,
                    "description": tk.description,
                    "status": tk.status if tk.status else "not started",
                    "agent": tk.agent.name if tk.agent else "No agent"
                }
                tasks_summary.append(task_info)
                logging.info(f"Task {tid} status: {task_info}")

            manager_prompt = f"""
Here is the current status of all tasks except yours (manager_task):
{tasks_summary}

Provide a JSON with the structure:
{{
   "task_id": <int>,
   "agent_name": "<string>",
   "action": "<execute or stop>"
}}
"""

            try:
                logging.info("Requesting manager instructions...")
                if manager_task.async_execution:
                    manager_response = await client.beta.chat.completions.parse(
                        model=self.manager_llm,
                        messages=[
                            {"role": "system", "content": manager_task.description},
                            {"role": "user", "content": manager_prompt}
                        ],
                        temperature=0.7,
                        response_format=ManagerInstructions
                    )
                else:
                    manager_response = client.beta.chat.completions.parse(
                        model=self.manager_llm,
                        messages=[
                            {"role": "system", "content": manager_task.description},
                            {"role": "user", "content": manager_prompt}
                        ],
                        temperature=0.7,
                        response_format=ManagerInstructions
                    )
                parsed_instructions = manager_response.choices[0].message.parsed
                logging.info(f"Manager instructions: {parsed_instructions}")
            except Exception as e:
                display_error(f"Manager parse error: {e}")
                logging.error(f"Manager parse error: {str(e)}", exc_info=True)
                break

            selected_task_id = parsed_instructions.task_id
            selected_agent_name = parsed_instructions.agent_name
            action = parsed_instructions.action

            logging.info(f"Manager selected task_id={selected_task_id}, agent={selected_agent_name}, action={action}")

            if action.lower() == "stop":
                logging.info("Manager decided to stop task execution")
                break

            if selected_task_id not in self.tasks:
                error_msg = f"Manager selected invalid task id {selected_task_id}"
                display_error(error_msg)
                logging.error(error_msg)
                break

            original_agent = self.tasks[selected_task_id].agent.name if self.tasks[selected_task_id].agent else "None"
            for a in self.agents:
                if a.name == selected_agent_name:
                    self.tasks[selected_task_id].agent = a
                    logging.info(f"Changed agent for task {selected_task_id} from {original_agent} to {selected_agent_name}")
                    break

            if self.tasks[selected_task_id].status != "completed":
                logging.info(f"Starting execution of task {selected_task_id}")
                yield selected_task_id
                logging.info(f"Finished execution of task {selected_task_id}, status: {self.tasks[selected_task_id].status}")

            if self.tasks[selected_task_id].status == "completed":
                completed_count += 1
                logging.info(f"Task {selected_task_id} completed. Total completed: {completed_count}/{total_tasks}")

        self.tasks[manager_task.id].status = "completed"
        if self.verbose >= 1:
            logging.info("All tasks completed under manager supervision.")
        logging.info("Hierarchical task execution finished")

    def workflow(self):
        """Synchronous version of workflow method"""
        current_iter = 0  # Track how many times we've looped
        # Build workflow relationships first
        for task in self.tasks.values():
            if task.next_tasks:
                for next_task_name in task.next_tasks:
                    next_task = next((t for t in self.tasks.values() if t.name == next_task_name), None)
                    if next_task:
                        next_task.previous_tasks.append(task.name)

        # Find start task
        start_task = None
        for task_id, task in self.tasks.items():
            if task.is_start:
                start_task = task
                break

        if not start_task:
            start_task = list(self.tasks.values())[0]
            logging.info("No start task marked, using first task")

        # If loop type and no input_file, default to tasks.csv
        if start_task and start_task.task_type == "loop" and not start_task.input_file:
            start_task.input_file = "tasks.csv"

        # --- If loop + input_file, read file & create tasks
        if start_task and start_task.task_type == "loop" and getattr(start_task, "input_file", None):
            try:
                file_ext = os.path.splitext(start_task.input_file)[1].lower()
                new_tasks = []

                if file_ext == ".csv":
                    with open(start_task.input_file, "r", encoding="utf-8") as f:
                        reader = csv.reader(f, quotechar='"', escapechar='\\')  # Handle quoted/escaped fields
                        previous_task = None
                        task_count = 0

                        for i, row in enumerate(reader):
                            if not row:  # Skip truly empty rows
                                continue

                            # Properly handle Q&A pairs with potential commas
                            task_desc = row[0].strip() if row else ""
                            if len(row) > 1:
                                # Preserve all fields in case of multiple commas
                                question = row[0].strip()
                                answer = ",".join(field.strip() for field in row[1:])
                                task_desc = f"Question: {question}\nAnswer: {answer}"

                            if not task_desc:  # Skip rows with empty content
                                continue

                            task_count += 1
                            logging.debug(f"Processing CSV row {i+1}: {task_desc}")

                            # Inherit next_tasks from parent loop task
                            inherited_next_tasks = start_task.next_tasks if start_task.next_tasks else []

                            row_task = Task(
                                description=f"{start_task.description}\n{task_desc}" if start_task.description else task_desc,
                                agent=start_task.agent,
                                name=f"{start_task.name}_{task_count}" if start_task.name else task_desc,
                                expected_output=getattr(start_task, 'expected_output', None),
                                is_start=(task_count == 1),
                                task_type="decision",  # Change to decision type
                                next_tasks=inherited_next_tasks,  # Inherit parent's next tasks
                                condition={
                                    "done": inherited_next_tasks if inherited_next_tasks else ["next"],  # Use full inherited_next_tasks
                                    "retry": ["current"],
                                    "exit": []  # Empty list for exit condition
                                }
                            )
                            self.tasks[row_task.id] = row_task
                            new_tasks.append(row_task)

                            if previous_task:
                                previous_task.next_tasks = [row_task.name]
                                previous_task.condition["done"] = [row_task.name]  # Use "done" consistently
                            previous_task = row_task

                            # For the last task in the loop, ensure it points to parent's next tasks
                            if task_count > 0 and not row_task.next_tasks:
                                row_task.next_tasks = inherited_next_tasks

                        logging.info(f"Processed {task_count} rows from CSV file")
                else:
                    # If not CSV, read lines
                    with open(start_task.input_file, "r", encoding="utf-8") as f:
                        lines = f.read().splitlines()
                        previous_task = None
                        for i, line in enumerate(lines):
                            row_task = Task(
                                description=f"{start_task.description}\n{line.strip()}" if start_task.description else line.strip(),
                                agent=start_task.agent,
                                name=f"{start_task.name}_{i+1}" if start_task.name else line.strip(),
                                expected_output=getattr(start_task, 'expected_output', None),
                                is_start=(i == 0),
                                task_type="task",
                                condition={
                                    "complete": ["next"],
                                    "retry": ["current"]
                                }
                            )
                            self.tasks[row_task.id] = row_task
                            new_tasks.append(row_task)

                            if previous_task:
                                previous_task.next_tasks = [row_task.name]
                                previous_task.condition["complete"] = [row_task.name]
                            previous_task = row_task

                if new_tasks:
                    start_task = new_tasks[0]
                    logging.info(f"Created {len(new_tasks)} tasks from: {start_task.input_file}")
            except Exception as e:
                logging.error(f"Failed to read file tasks: {e}")

        # end of start task handling
        current_task = start_task
        visited_tasks = set()
        loop_data = {}  # Store loop-specific data

        while current_task:
            current_iter += 1
            if current_iter > self.max_iter:
                logging.info(f"Max iteration limit {self.max_iter} reached, ending workflow.")
                break

            # Add task summary at start of each cycle
            logging.debug(f"""
=== Workflow Cycle {current_iter} Summary ===
Total tasks: {len(self.tasks)}
Outstanding tasks: {sum(1 for t in self.tasks.values() if t.status != "completed")}
Completed tasks: {sum(1 for t in self.tasks.values() if t.status == "completed")}
Tasks by status:
- Not started: {sum(1 for t in self.tasks.values() if t.status == "not started")}
- In progress: {sum(1 for t in self.tasks.values() if t.status == "in_progress")}
- Completed: {sum(1 for t in self.tasks.values() if t.status == "completed")}
Tasks by type:
- Loop tasks: {sum(1 for t in self.tasks.values() if t.task_type == "loop")}
- Decision tasks: {sum(1 for t in self.tasks.values() if t.task_type == "decision")}
- Regular tasks: {sum(1 for t in self.tasks.values() if t.task_type not in ["loop", "decision"])}
            """)

            # Handle loop task file reading at runtime
            if (current_task.task_type == "loop" and
                current_task is not start_task and
                getattr(current_task, "_subtasks_created", False) is not True):

                if not current_task.input_file:
                    current_task.input_file = "tasks.csv"

                if getattr(current_task, "input_file", None):
                    try:
                        file_ext = os.path.splitext(current_task.input_file)[1].lower()
                        new_tasks = []

                        if file_ext == ".csv":
                            with open(current_task.input_file, "r", encoding="utf-8") as f:
                                reader = csv.reader(f)
                                previous_task = None
                                for i, row in enumerate(reader):
                                    if row:  # Skip empty rows
                                        task_desc = row[0]  # Take first column
                                        row_task = Task(
                                            description=f"{current_task.description}\n{task_desc}" if current_task.description else task_desc,
                                            agent=current_task.agent,
                                            name=f"{current_task.name}_{i+1}" if current_task.name else task_desc,
                                            expected_output=getattr(current_task, 'expected_output', None),
                                            is_start=(i == 0),
                                            task_type="task",
                                            condition={
                                                "complete": ["next"],
                                                "retry": ["current"]
                                            }
                                        )
                                        self.tasks[row_task.id] = row_task
                                        new_tasks.append(row_task)

                                        if previous_task:
                                            previous_task.next_tasks = [row_task.name]
                                            previous_task.condition["complete"] = [row_task.name]
                                        previous_task = row_task
                        else:
                            with open(current_task.input_file, "r", encoding="utf-8") as f:
                                lines = f.read().splitlines()
                                previous_task = None
                                for i, line in enumerate(lines):
                                    row_task = Task(
                                        description=f"{current_task.description}\n{line.strip()}" if current_task.description else line.strip(),
                                        agent=current_task.agent,
                                        name=f"{current_task.name}_{i+1}" if current_task.name else line.strip(),
                                        expected_output=getattr(current_task, 'expected_output', None),
                                        is_start=(i == 0),
                                        task_type="task",
                                        condition={
                                            "complete": ["next"],
                                            "retry": ["current"]
                                        }
                                    )
                                    self.tasks[row_task.id] = row_task
                                    new_tasks.append(row_task)

                                    if previous_task:
                                        previous_task.next_tasks = [row_task.name]
                                        previous_task.condition["complete"] = [row_task.name]
                                    previous_task = row_task

                        if new_tasks:
                            current_task.next_tasks = [new_tasks[0].name]
                            current_task._subtasks_created = True
                            logging.info(f"Created {len(new_tasks)} tasks from: {current_task.input_file} for loop task {current_task.name}")
                    except Exception as e:
                        logging.error(f"Failed to read file tasks for loop task {current_task.name}: {e}")

            task_id = current_task.id
            logging.debug(f"""
=== Task Execution Details ===
Current task: {current_task.name}
Type: {current_task.task_type}
Status: {current_task.status}
Previous tasks: {current_task.previous_tasks}
Next tasks: {current_task.next_tasks}
Context tasks: {[t.name for t in current_task.context] if current_task.context else []}
Description length: {len(current_task.description)}
            """)

            # Add context from previous tasks to description
            if current_task.previous_tasks or current_task.context:
                context = "\nInput data from previous tasks:"

                # Add data from previous tasks in workflow
                for prev_name in current_task.previous_tasks:
                    prev_task = next((t for t in self.tasks.values() if t.name == prev_name), None)
                    if prev_task and prev_task.result:
                        # Handle loop data
                        if current_task.task_type == "loop":
                            context += f"\n{prev_name}: {prev_task.result.raw}"
                        else:
                            context += f"\n{prev_name}: {prev_task.result.raw}"

                # Add data from context tasks
                if current_task.context:
                    for ctx_task in current_task.context:
                        if ctx_task.result and ctx_task.name != current_task.name:
                            context += f"\n{ctx_task.name}: {ctx_task.result.raw}"

                # Update task description with context
                current_task.description = current_task.description + context

            # Skip execution for loop tasks, only process their subtasks
            if current_task.task_type == "loop":
                logging.debug(f"""
=== Loop Task Details ===
Name: {current_task.name}
ID: {current_task.id}
Status: {current_task.status}
Next tasks: {current_task.next_tasks}
Condition: {current_task.condition}
Subtasks created: {getattr(current_task, '_subtasks_created', False)}
Input file: {getattr(current_task, 'input_file', None)}
                """)

                # Check if subtasks are created and completed
                if getattr(current_task, "_subtasks_created", False):
                    subtasks = [
                        t for t in self.tasks.values()
                        if t.name.startswith(current_task.name + "_")
                    ]

                    logging.debug(f"""
=== Subtask Status Check ===
Total subtasks: {len(subtasks)}
Completed: {sum(1 for st in subtasks if st.status == "completed")}
Pending: {sum(1 for st in subtasks if st.status != "completed")}
                    """)

                    for st in subtasks:
                        logging.debug(f"""
Subtask: {st.name}
- Status: {st.status}
- Next tasks: {st.next_tasks}
- Condition: {st.condition}
                        """)

                    if subtasks and all(st.status == "completed" for st in subtasks):
                        logging.debug(f"=== All {len(subtasks)} subtasks completed for {current_task.name} ===")

                        # Mark loop task completed and move to next task
                        current_task.status = "completed"
                        logging.debug(f"Loop {current_task.name} marked as completed")

                        # Move to next task if available
                        if current_task.next_tasks:
                            next_task_name = current_task.next_tasks[0]
                            logging.debug(f"Attempting transition to next task: {next_task_name}")
                            next_task = next((t for t in self.tasks.values() if t.name == next_task_name), None)
                            if next_task:
                                logging.debug(f"=== Transitioning: {current_task.name} -> {next_task.name} ===")
                                logging.debug(f"Next task status: {next_task.status}")
                                logging.debug(f"Next task condition: {next_task.condition}")
                            current_task = next_task
                        else:
                            logging.debug(f"=== No next tasks for {current_task.name}, checking fallback ===")
                            current_task = self._find_next_not_started_task() # Fallback here only after loop completion
                else:
                    logging.debug(f"No subtasks created yet for {current_task.name}")
                    # Create subtasks if needed
                    if current_task.input_file:
                        self._create_loop_subtasks(current_task)
                        current_task._subtasks_created = True
                        logging.debug(f"Created subtasks from {current_task.input_file}")
                    else:
                        # No input file, mark as done
                        current_task.status = "completed"
                        logging.debug(f"No input file, marking {current_task.name} as completed")
                        if current_task.next_tasks:
                            next_task_name = current_task.next_tasks[0]
                            next_task = next((t for t in self.tasks.values() if t.name == next_task_name), None)
                            current_task = next_task
                        else:
                            current_task = None
            else:
                # Execute non-loop task
                logging.debug(f"=== Executing non-loop task: {current_task.name} (id: {task_id}) ===")
                logging.debug(f"Task status: {current_task.status}")
                logging.debug(f"Task next_tasks: {current_task.next_tasks}")
                yield task_id
                visited_tasks.add(task_id)

            # Reset completed task to "not started" so it can run again
            if self.tasks[task_id].status == "completed":
                # Never reset loop tasks, decision tasks, or their subtasks
                subtask_name = self.tasks[task_id].name
                logging.debug(f"=== Checking reset for completed task: {subtask_name} ===")
                logging.debug(f"Task type: {self.tasks[task_id].task_type}")
                logging.debug(f"Task status before reset check: {self.tasks[task_id].status}")

                if (self.tasks[task_id].task_type not in ["loop", "decision"] and
                    not any(t.task_type == "loop" and subtask_name.startswith(t.name + "_")
                           for t in self.tasks.values())):
                    logging.debug(f"=== Resetting non-loop, non-decision task {subtask_name} to 'not started' ===")
                    self.tasks[task_id].status = "not started"
                    logging.debug(f"Task status after reset: {self.tasks[task_id].status}")
                else:
                    logging.debug(f"=== Skipping reset for loop/decision/subtask: {subtask_name} ===")
                    logging.debug(f"Keeping status as: {self.tasks[task_id].status}")

            # Handle loop progression
            if current_task.task_type == "loop":
                loop_key = f"loop_{current_task.name}"
                if loop_key in loop_data:
                    loop_info = loop_data[loop_key]
                    loop_info["index"] += 1
                    has_more = loop_info["remaining"] > 0

                    # Update result to trigger correct condition
                    if current_task.result:
                        result = current_task.result.raw
                        if has_more:
                            result += "\nmore"
                        else:
                            result += "\ndone"
                        current_task.result.raw = result

            # Determine next task based on result
            next_task = None
            if current_task and current_task.result:
                if current_task.task_type in ["decision", "loop"]:
                    # MINIMAL CHANGE: use pydantic decision if present
                    decision_str = current_task.result.raw.lower()
                    if current_task.result.pydantic and hasattr(current_task.result.pydantic, "decision"):
                        decision_str = current_task.result.pydantic.decision.lower()

                    # Check conditions
                    for condition, tasks in current_task.condition.items():
                        if condition.lower() == decision_str:
                            # Handle both list and direct string values
                            task_value = tasks[0] if isinstance(tasks, list) else tasks
                            if not task_value or task_value == "exit":
                                logging.info("Workflow exit condition met, ending workflow")
                                current_task = None
                                break
                            next_task_name = task_value
                            next_task = next((t for t in self.tasks.values() if t.name == next_task_name), None)
                            # For loops, allow revisiting the same task
                            if next_task and next_task.id == current_task.id:
                                visited_tasks.discard(current_task.id)
                            break

            if not next_task and current_task and current_task.next_tasks:
                next_task_name = current_task.next_tasks[0]
                next_task = next((t for t in self.tasks.values() if t.name == next_task_name), None)

            current_task = next_task
            if not current_task:
                current_task = self._find_next_not_started_task() # General fallback if no next task in workflow


            if not current_task:
                # Add final workflow summary
                logging.debug(f"""
=== Final Workflow Summary ===
Total tasks processed: {len(self.tasks)}
Final status:
- Completed tasks: {sum(1 for t in self.tasks.values() if t.status == "completed")}
- Outstanding tasks: {sum(1 for t in self.tasks.values() if t.status != "completed")}
Tasks by status:
- Not started: {sum(1 for t in self.tasks.values() if t.status == "not started")}
- In progress: {sum(1 for t in self.tasks.values() if t.status == "in_progress")}
- Completed: {sum(1 for t in self.tasks.values() if t.status == "completed")}
- Failed: {sum(1 for t in self.tasks.values() if t.status == "failed")}
Tasks by type:
- Loop tasks: {sum(1 for t in self.tasks.values() if t.task_type == "loop")}
- Decision tasks: {sum(1 for t in self.tasks.values() if t.task_type == "decision")}
- Regular tasks: {sum(1 for t in self.tasks.values() if t.task_type not in ["loop", "decision"])}
Total iterations: {current_iter}
                """)

                logging.info("Workflow execution completed")
                break

            # Add completion logging
            logging.debug(f"""
=== Task Completion ===
Task: {current_task.name}
Final status: {current_task.status}
Next task: {next_task.name if next_task else None}
Iteration: {current_iter}/{self.max_iter}
            """)

    def sequential(self):
        """Synchronous version of sequential method"""
        for task_id in self.tasks:
            if self.tasks[task_id].status != "completed":
                yield task_id

    def hierarchical(self):
        """Synchronous version of hierarchical method"""
        logging.debug(f"Starting hierarchical task execution with {len(self.tasks)} tasks")
        manager_agent = Agent(
            name="Manager",
            role="Project manager",
            goal="Manage the entire flow of tasks and delegate them to the right agent",
            backstory="Expert project manager to coordinate tasks among agents",
            llm=self.manager_llm,
            verbose=self.verbose,
            markdown=True,
            self_reflect=False
        )

        class ManagerInstructions(BaseModel):
            task_id: int
            agent_name: str
            action: str

        manager_task = Task(
            name="manager_task",
            description="Decide the order of tasks and which agent executes them",
            expected_output="All tasks completed successfully",
            agent=manager_agent
        )
        manager_task_id = yield manager_task
        logging.info(f"Created manager task with ID {manager_task_id}")

        completed_count = 0
        total_tasks = len(self.tasks) - 1
        logging.info(f"Need to complete {total_tasks} tasks (excluding manager task)")

        while completed_count < total_tasks:
            tasks_summary = []
            for tid, tk in self.tasks.items():
                if tk.name == "manager_task":
                    continue
                task_info = {
                    "task_id": tid,
                    "name": tk.name,
                    "description": tk.description,
                    "status": tk.status if tk.status else "not started",
                    "agent": tk.agent.name if tk.agent else "No agent"
                }
                tasks_summary.append(task_info)
                logging.info(f"Task {tid} status: {task_info}")

            manager_prompt = f"""
Here is the current status of all tasks except yours (manager_task):
{tasks_summary}

Provide a JSON with the structure:
{{
   "task_id": <int>,
   "agent_name": "<string>",
   "action": "<execute or stop>"
}}
"""

            try:
                logging.info("Requesting manager instructions...")
                manager_response = client.beta.chat.completions.parse(
                    model=self.manager_llm,
                    messages=[
                        {"role": "system", "content": manager_task.description},
                        {"role": "user", "content": manager_prompt}
                    ],
                    temperature=0.7,
                    response_format=ManagerInstructions
                )
                parsed_instructions = manager_response.choices[0].message.parsed
                logging.info(f"Manager instructions: {parsed_instructions}")
            except Exception as e:
                display_error(f"Manager parse error: {e}")
                logging.error(f"Manager parse error: {str(e)}", exc_info=True)
                break

            selected_task_id = parsed_instructions.task_id
            selected_agent_name = parsed_instructions.agent_name
            action = parsed_instructions.action

            logging.info(f"Manager selected task_id={selected_task_id}, agent={selected_agent_name}, action={action}")

            if action.lower() == "stop":
                logging.info("Manager decided to stop task execution")
                break

            if selected_task_id not in self.tasks:
                error_msg = f"Manager selected invalid task id {selected_task_id}"
                display_error(error_msg)
                logging.error(error_msg)
                break

            original_agent = self.tasks[selected_task_id].agent.name if self.tasks[selected_task_id].agent else "None"
            for a in self.agents:
                if a.name == selected_agent_name:
                    self.tasks[selected_task_id].agent = a
                    logging.info(f"Changed agent for task {selected_task_id} from {original_agent} to {selected_agent_name}")
                    break

            if self.tasks[selected_task_id].status != "completed":
                logging.info(f"Starting execution of task {selected_task_id}")
                yield selected_task_id
                logging.info(f"Finished execution of task {selected_task_id}, status: {self.tasks[selected_task_id].status}")

            if self.tasks[selected_task_id].status == "completed":
                completed_count += 1
                logging.info(f"Task {selected_task_id} completed. Total completed: {completed_count}/{total_tasks}")

        self.tasks[manager_task.id].status = "completed"
        if self.verbose >= 1:
            logging.info("All tasks completed under manager supervision.")
        logging.info("Hierarchical task execution finished")