# Wake-gate fixture (P14-09): wake the agent and hand it some context.
import json

print("build #812 is red")
print(json.dumps({"wake": True, "context": {"build": 812, "state": "red"}}))
