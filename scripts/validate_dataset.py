import json
from data_validation.data_quality_report import quality_report
if __name__=="__main__": print(json.dumps(quality_report(),indent=2))
