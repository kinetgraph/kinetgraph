import os
import sys
import glob
import subprocess
import time


def main():
    print("Executing all examples with real Redis and FalkorDB...")

    examples = sorted(glob.glob("examples/[0-1]*.py"))

    success = []
    failed = []

    for ex in examples:
        print(f"\n--- Running {ex} ---")
        start = time.time()

        try:
            env = os.environ.copy()
            env.pop("FMH_REDIS_FAKE", None)
            env["FMH_REDIS_URL"] = "redis://:redispassword@127.0.0.1:6379"
            env["FMH_EMBEDDING_TIMEOUT_SECONDS"] = "120.0"

            result = subprocess.run(
                ["uv", "run", "--package", "kntgraph", "python", ex],
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )

            elapsed = time.time() - start

            if result.returncode == 0:
                print(f"✅ OK ({elapsed:.1f}s)")
                success.append(ex)
            else:
                print(f"❌ FAILED ({elapsed:.1f}s) - Exit code: {result.returncode}")
                print("Stderr tail:")
                lines = result.stderr.strip().split("\n")
                for line in lines[-20:]:
                    print(f"  {line}")
                failed.append(ex)

        except subprocess.TimeoutExpired as e:
            elapsed = time.time() - start
            print(f"⏱️ TIMEOUT ({elapsed:.1f}s)")
            if e.stderr:
                lines = e.stderr.decode("utf-8", errors="replace").strip().split("\n")
                for line in lines[-10:]:
                    print(f"  {line}")
            failed.append(ex)

    print("\n" + "=" * 50)
    print("SUMMARY")
    print("=" * 50)
    print(f"Total: {len(examples)}")
    print(f"Success: {len(success)}")
    print(f"Failed: {len(failed)}")

    if failed:
        print("\nFailed examples:")
        for ex in failed:
            print(f"  - {ex}")
        sys.exit(1)
    else:
        print("\nAll examples executed successfully!")
        sys.exit(0)


if __name__ == "__main__":
    main()
