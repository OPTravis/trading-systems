#!/home/travis/crypto-ai-trader/.venv/bin/python3
"""
預部署驗證門檻 — 任何代碼修改前必須通過此腳本

用法：
  python3 scripts/validate_before_deploy.py [file_to_validate]

退出碼：
  0 = 全部通過，可以部署
  1 = 有失敗，阻止部署
"""

import subprocess
import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_FILE = os.path.join(PROJECT_ROOT, 'tests', 'test_regression.py')

def run_tests(target_file=None):
    """執行回歸測試"""
    cmd = [
        sys.executable, '-m', 'pytest',
        TEST_FILE,
        '-v', '--tb=short',
        '-x',  # 第一個失敗就停止
        '-m', 'not slow',  # 跳過慢速測試
    ]

    result = subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120
    )

    return result.returncode == 0, result.stdout, result.stderr

def main():
    target_file = sys.argv[1] if len(sys.argv) > 1 else None

    print("=" * 60)
    print("🔍 預部署驗證")
    print("=" * 60)

    if target_file:
        print(f"📁 目標文件: {target_file}")

    print("\n📋 執行回歸測試...")
    passed, stdout, stderr = run_tests(target_file)

    if passed:
        print("✅ 全部通過，可以部署")
        print(f"\n{stdout.split('===')[-1].strip() if '===' in stdout else ''}")
        sys.exit(0)
    else:
        print("❌ 測試失敗，阻止部署")
        print(f"\n{stderr[-500:] if stderr else ''}")
        print(f"\n{stdout[-500:] if stdout else ''}")
        sys.exit(1)

if __name__ == '__main__':
    main()
