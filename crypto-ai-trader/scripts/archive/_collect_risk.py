from src.risk_manager import RiskManager
import json

rm = RiskManager()
status = rm.get_full_status()
print(json.dumps(status, indent=2, default=str))
