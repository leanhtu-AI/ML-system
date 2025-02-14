import argparse
import logging
import os
import random
import time

import mlflow
import pandas as pd
import uvicorn
import yaml
from fastapi import FastAPI, Request
from pandas.util import hash_pandas_object
from pydantic import BaseModel

from problem_config import ProblemConst, create_prob_config
from raw_data_processor1 import RawDataProcessor as RawDataProcessor1
from raw_data_processor2 import RawDataProcessor as RawDataProcessor2
from utils import AppConfig, AppPath

PREDICTOR_API_PORT = 8000


class Data(BaseModel):
    id: str
    rows: list
    columns: list


class ModelPredictor:
    def __init__(self, config_file_path):
        config_file_path_specific = {
            "prob-1": "prob-1/model-1.yaml",
            "prob-2": "prob-2/model-1.yaml",
        }
        self.model = {}
        self.config = {}
        self.category_index = {}
        self.prob_config = {}

        for prob in ["prob-1"]:
            with open(
                os.path.join(config_file_path, config_file_path_specific[prob]), "r"
            ) as f:
                self.config[prob] = yaml.safe_load(f)
            logging.info(f"model-config: {self.config[prob]}")

            mlflow.set_tracking_uri(AppConfig.MLFLOW_TRACKING_URI)

            self.prob_config[prob] = create_prob_config(
                self.config[prob]["phase_id"], self.config[prob]["prob_id"]
            )

            # load category_index
            self.category_index[prob] = RawDataProcessor1.load_category_index(
                self.prob_config[prob]
            )

            # load model
            model_uri = os.path.join(
                "models:/",
                self.config[prob]["model_name"],
                str(self.config[prob]["model_version"]),
            )
            model_uri = model_uri.replace("\\", "/")
            self.model[prob] = mlflow.pyfunc.load_model(model_uri)

    def detect_drift(self, feature_df) -> int:
        # watch drift between coming requests and training data
        time.sleep(0.02)
        return random.choice([0, 1])

    def predict(self, data: Data, prob="prob-1"):
        start_time = time.time()

        # preprocess
        raw_df = pd.DataFrame(data.rows, columns=data.columns)
        if prob == "prob-1":
            feature_df = RawDataProcessor1.apply_category_features(
                raw_df=raw_df,
                categorical_cols=self.prob_config[prob].categorical_cols,
                category_index=self.category_index[prob],
            )
        else:
            feature_df = RawDataProcessor2.apply_category_features(
                raw_df=raw_df,
                categorical_cols=self.prob_config[prob].categorical_cols,
                category_index=self.category_index[prob],
            )
        # save request data for improving models
        ModelPredictor.save_request_data(
            feature_df, self.prob_config[prob].captured_data_dir, data.id
        )

        prediction = self.model[prob].predict(feature_df)
        is_drifted = self.detect_drift(feature_df)

        run_time = round((time.time() - start_time) * 1000, 0)
        logging.info(f"prediction takes {run_time} ms")
        return {
            "id": data.id,
            "predictions": prediction.tolist(),
            "drift": is_drifted,
        }

    @staticmethod
    def save_request_data(feature_df: pd.DataFrame, captured_data_dir, data_id: str):
        if data_id.strip():
            filename = data_id
        else:
            filename = hash_pandas_object(feature_df).sum()
        output_file_path = os.path.join(captured_data_dir, f"{filename}.parquet")
        feature_df.to_parquet(output_file_path, index=False)
        return output_file_path


class PredictorApi:
    def __init__(self, predictor: ModelPredictor):
        self.predictor = predictor
        self.app = FastAPI()

        @self.app.get("/")
        async def root():
            return {"message": "hello"}

        @self.app.post("/phase-1/prob-1/predict")
        async def predict(data: Data, request: Request):
            self._log_request(request)
            response = self.predictor.predict(data, prob="prob-1")
            self._log_response(response)
            return response

        # @self.app.post("/phase-1/prob-2/predict")
        # async def predict(data: Data, request: Request):
        #     self._log_request(request)
        #     response = self.predictor.predict(data, prob="prob-2")
        #     self._log_response(response)
        #     return response

    @staticmethod
    def _log_request(request: Request):
        pass

    @staticmethod
    def _log_response(response: dict):
        pass

    def run(self, port):
        uvicorn.run(self.app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    default_config_path = (
        AppPath.MODEL_CONFIG_DIR
        / ProblemConst.PHASE1
        / ProblemConst.PROB1
        / "model-1.yaml"
    ).as_posix()

    parser = argparse.ArgumentParser()
    parser.add_argument("--config-path", type=str, default=default_config_path)
    parser.add_argument("--port", type=int, default=PREDICTOR_API_PORT)
    args = parser.parse_args()

    predictor = ModelPredictor(config_file_path=args.config_path)
    api = PredictorApi(predictor)
    api.run(port=args.port)
