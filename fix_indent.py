"""Fix indentation in collections.py"""
with open("src/backend/api/v1/collections.py", "r") as f:
    lines = f.readlines()

fixed_lines = [
    "    # ---------------------------------------------------------\n",
    "    # 2. Create collection job\n",
    "    # ---------------------------------------------------------\n",
    "\n",
    "    # Clean up any stale RUNNING jobs for this match so the unique constraint\n",
    "    # is not violated on re-runs.\n",
    "    from sqlalchemy import text as sa_text\n",
    "\n",
    "    await session.execute(\n",
    "        sa_text(\n",
    '            "DELETE FROM jobs "\n',
    '            "WHERE source_id = :sid "\n',
    '            "AND status = :running"\n',
    "        ),\n",
    "        {\n",
    '            "sid": str(source_id_val),\n',
    '            "running": JobStatusEnum.RUNNING.value,\n',
    "        },\n",
    "    )\n",
    "    await session.flush()\n",
    "\n",
]

lines[129:149] = fixed_lines

with open("src/backend/api/v1/collections.py", "w") as f:
    f.writelines(lines)

print("Fixed")