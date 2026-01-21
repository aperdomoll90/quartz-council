"""
Test script for QuartzCouncil agents.
Run with: uv run python test_council.py
"""
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent / "src"))

from dotenv import load_dotenv
load_dotenv()

from quartzcouncil.agents import create_quartz

# Sample PR diff with intentional issues for both agents to catch
SAMPLE_DIFF = '''
diff --git a/src/components/UserList.tsx b/src/components/UserList.tsx
new file mode 100644
index 0000000..abc1234
--- /dev/null
+++ b/src/components/UserList.tsx
@@ -0,0 +1,45 @@
+import { useState, useEffect } from 'react';
+
+// Type issues for Amethyst to catch
+interface User {
+  id: number;
+  name: string;
+}
+
+export function UserList({ onSelect }) {  // Missing prop types
+  const [users, setUsers] = useState<any[]>([]);  // Using any
+  const [loading, setLoading] = useState(false);
+
+  // Effect issues for Citrine to catch
+  useEffect(() => {
+    const controller = new AbortController();
+
+    fetch('/api/users', { signal: controller.signal })
+      .then(res => res.json())
+      .then(data => {
+        setUsers(data as User[]);  // Unsafe cast
+      });
+
+    // Missing cleanup: controller.abort()
+  }, []);  // Missing dependency if onSelect is used
+
+  // Performance issue: inline function in render
+  const handleClick = (user: any) => {
+    onSelect(user);
+  };
+
+  // Another effect with missing cleanup
+  useEffect(() => {
+    window.addEventListener('resize', () => {
+      console.log('resized');
+    });
+    // Missing removeEventListener cleanup
+  }, []);
+
+  return (
+    <div>
+      {users.map(user => (
+        <button key={user.id} onClick={() => handleClick(user)}>
+          {user.name}
+        </button>
+      ))}
+    </div>
+  );
+}
'''

def main():
    print("Creating Quartz council...")
    quartz = create_quartz(model="gpt-4o")

    print("Running review on sample diff...")
    print("-" * 60)

    result = quartz(SAMPLE_DIFF)

    print("\n=== REVIEW COMMENTS ===\n")
    for i, comment in enumerate(result.comments, 1):
        print(f"{i}. [{comment.agent}] {comment.severity.upper()}")
        print(f"   File: {comment.file}:{comment.line_start}-{comment.line_end}")
        print(f"   Category: {comment.category}")
        print(f"   {comment.message}")
        if comment.suggestion:
            print(f"   Suggestion: {comment.suggestion}")
        print()

    print("=== SUMMARY ===\n")
    print(result.summary)

if __name__ == "__main__":
    main()
