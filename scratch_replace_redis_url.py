import glob

for filepath in glob.glob("examples/*.py") + glob.glob("examples/_lib/*.py"):
    with open(filepath, "r") as f:
        content = f.read()

    # Replace "redis://localhost:6379" with "redis://:redispassword@localhost:6379"
    new_content = content.replace(
        '"redis://localhost:6379"', '"redis://:redispassword@localhost:6379"'
    )

    if new_content != content:
        with open(filepath, "w") as f:
            f.write(new_content)
        print(f"Updated {filepath}")
