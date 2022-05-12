from os import path, makedirs
import shutil
from datetime import datetime
from constants import *
import pandas as pd
from utilities.data import get_classes
from utilities.feature_selection import select_categorical_features
from utilities.preprocess import get_feature_preprocessor, label_encode
from airflow import DAG
from airflow.operators.python import get_current_context
from airflow.operators.python_operator import PythonOperator
from airflow.providers.sqlite.operators.sqlite import SqliteOperator
import pickle

index_column = index_col.lower()

f_names = [train_fname, val_fname, test_fname]
keys = ["train", "validation", "test"]

default_args = dict(
    owner="airflow",
    start_date=datetime(2022, 1, 1),
    depends_on_past=False,
    email_on_failure=False,
    email_on_retry=False,
    schedule_interval="@daily",
    max_active_runs=1,  # no concurrent runs
    catchup=False,  # to not auto run dag
)


def get_xcom_values(task_ids):
    return get_current_context()["ti"].xcom_pull(task_ids=task_ids)


def create_temp_dir():
    temp_dir_name = path.abspath(f"temp_{datetime.now().strftime('%Y%m%d%H%M%S')}")
    makedirs(temp_dir_name)

    return temp_dir_name


# read data, drop redundant columns, save data to temp directory
def read_data():
    path_dict = {}
    temp_dir_name = get_xcom_values(temp_dir_task_id)
    for f_name, key in zip(f_names, keys):
        data_path = path.join(data_dir, f_name)
        dest_path = path.join(temp_dir_name, f_name)
        data = pd.read_csv(data_path)
        data.drop(columns=coliner_col, inplace=True)
        data.to_csv(dest_path, **save_config)
        path_dict[key] = dest_path
    return path_dict


# select categorical features from train set
def select_features():
    temp_train_path = get_xcom_values(read_data_task_id)["train"]
    train = pd.read_csv(temp_train_path, **read_config)

    features, target = train.drop(columns=[target_col]), train[target_col]
    cat_cols = features.select_dtypes(include=["object"]).columns.tolist()
    # use all numerical columns because majority of them are non-normaly
    # distributed (don't meet selection test criteria)
    return select_categorical_features(features, target, cat_cols)


# create output directory for preprocessing result if needed
def create_output_dir(prep_dir: str):
    prep_dir_path = path.join(output_dir, prep_dir)
    makedirs(prep_dir_path, exist_ok=True)


def create_preprocessor(prep_dir: str, std=True):
    cat_features = get_xcom_values(select_features_task_id)
    temp_train_path = get_xcom_values(read_data_task_id)["train"]
    train = pd.read_csv(temp_train_path, **read_config)

    num_cols = (
        train.drop(columns=[target_col])
        .select_dtypes(exclude=["object"])
        .columns.tolist()
    )
    preprocessor, col_names = get_feature_preprocessor(
        train, cat_features, num_cols, std
    )
    prep_dir_path = path.join(output_dir, prep_dir)
    with open(path.join(prep_dir_path, preprocessor_fname), "wb") as out_file:
        pickle.dump(preprocessor, out_file)

    return col_names


def preprocess(prep_dir: str, create_prep_task_id: str):
    index_cols = [index_column]
    prep_dir_path = path.join(output_dir, prep_dir)
    data_path = get_xcom_values(read_data_task_id)
    col_names = get_xcom_values(create_prep_task_id)

    with open(path.join(prep_dir_path, preprocessor_fname), "rb") as in_file:
        preprocessor = pickle.load(in_file)

    train = pd.read_csv(data_path["train"], **read_config)
    val = pd.read_csv(data_path["validation"], **read_config)
    test = pd.read_csv(data_path["test"], **read_config)
    parts = [train, val, test]
    classes = get_classes(train, target_col)

    for f_name, part in zip(f_names, parts):
        data_out = pd.DataFrame(part.index.values, columns=index_cols)
        data_out[target_col] = label_encode(part[target_col], classes).values
        data_out[col_names] = preprocessor.transform(part)
        data_out.to_csv(path.join(prep_dir_path, f_name), **save_config)


def remove_temp_dir():
    temp_dir_name = get_xcom_values(temp_dir_task_id)
    shutil.rmtree(temp_dir_name)


dag = DAG(
    "preprocessing_dag",
    default_args=default_args,
)

create_temp_dir_task = PythonOperator(
    task_id=temp_dir_task_id, python_callable=create_temp_dir, dag=dag
)

read_data_task = PythonOperator(
    task_id=read_data_task_id, python_callable=read_data, dag=dag
)

select_features_task = PythonOperator(
    task_id=select_features_task_id, python_callable=select_features, dag=dag
)

create_std_dir_task = PythonOperator(
    task_id=std_dir_task_id,
    python_callable=create_output_dir,
    op_args=[std_dir],
    dag=dag,
)

create_minmax_dir_task = PythonOperator(
    task_id=minmax_dir_task_id,
    python_callable=create_output_dir,
    op_args=[minmax_dir],
    dag=dag,
)

create_prep_std_task = PythonOperator(
    task_id=create_prep_std_task_id,
    python_callable=create_preprocessor,
    op_args=[std_dir],
    dag=dag,
)

create_prep_minmax_task = PythonOperator(
    task_id=create_prep_minmax_task_id,
    python_callable=create_preprocessor,
    op_args=[minmax_dir],
    op_kwargs={"std": False},
    dag=dag,
)

preprocess_std_task = PythonOperator(
    task_id=preprocess_std_task_id,
    python_callable=preprocess,
    op_args=[std_dir, create_prep_std_task_id],
    dag=dag,
)

preprocess_minmax_task = PythonOperator(
    task_id=preprocess_minmax_task_id,
    python_callable=preprocess,
    op_args=[minmax_dir, create_prep_minmax_task_id],
    dag=dag,
)

remove_temp_dir_task = PythonOperator(
    task_id=remove_temp_dir_task_id, python_callable=remove_temp_dir, dag=dag
)

cleanup_task = SqliteOperator(
    task_id=cleanup_task_id,
    sqlite_conn_id=sqlite_conn_id,
    sql=delete_xcom_sql.format(dag.dag_id),
    dag=dag,
)

create_temp_dir_task >> read_data_task
read_data_task >> [select_features_task, create_std_dir_task, create_minmax_dir_task]
create_std_dir_task >> create_prep_std_task >> preprocess_std_task
create_minmax_dir_task >> create_prep_minmax_task >> preprocess_minmax_task
[preprocess_std_task, preprocess_minmax_task] >> remove_temp_dir_task >> cleanup_task

# def split_data():
#     temp_dir_name = get_xcom_values(temp_dir_task_id)
#     temp_file_path = get_xcom_values(read_data_task_id)
#     data = pd.read_csv(temp_file_path)

#     train, test = train_test_split(data, **split_config, stratify=data[target_col])
#     train, val = train_test_split(train, **split_config, stratify=train[target_col])

#     parts = [train, val, test]
#     return_dict = {}

#     for f_name, part, key in zip(f_names, parts, keys):
#         temp_path = get_file_name(temp_dir_name, f_name)
#         part.to_csv(temp_path, **save_config)
#         return_dict[key] = temp_path

#     return return_dict
# temp_train_path = get_file_name(temp_dir_name, train_fname)
# temp_val_path = get_file_name(temp_dir_name, val_fname)
# temp_test_path = get_file_name(temp_dir_name, test_fname)

# train.to_csv(temp_train_path, **save_config)
# val.to_csv(temp_val_path, **save_config)
# test.to_csv(temp_test_path, **save_config)

# return {"train": temp_train_path, "val": temp_val_path, "test": temp_test_path}


# train_out = pd.DataFrame(train.index.values, columns=index_cols)
# val_out = pd.DataFrame(val.index.values, columns=index_cols)
# test_out = pd.DataFrame(test.index.values, columns=index_cols)

# classes = get_classes(train, target_col)
# train_out[target_col] = label_encode(train[target_col], classes).values
# test_out[target_col] = label_encode(test[target_col], classes).values

# train_out[col_names] = preprocessor.transform(train)
# test_out[col_names] = preprocessor.transform(test)

# train_out.to_csv(get_out_path(out_dir, train_fname), index=False)
# test_out.to_csv(get_out_path(out_dir, test_fname), index=False)
