import logging
import kubernetes
from kubernetes.client.rest import ApiException
import yaml
import os
import json
import datetime
from random import choice 

kubernetes.config.load_kube_config()
api = kubernetes.client.CustomObjectsApi()

def generate_hpaprescaler_objects(name_prefix, app_name_list, profile_name_list,namespace, obj_count, time_between_mins):
    current_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=20)

    for idx in range(obj_count):
        current_time += datetime.timedelta(minutes=time_between_mins)
        argo_app_name = app_name_list[idx%len(app_name_list)]
        profile_name = profile_name_list[idx%len(profile_name_list)]
        obj_name = f"{name_prefix}-{str(idx)}"
        
        body = {
            "apiVersion": "hepapi.com/v1",
            "kind": "HpaPrescaler",
            "metadata": {
                "name": obj_name,
                "namespace": namespace
            },
            "spec":{
                "argocdAppName": argo_app_name,
                "timeStart": current_time.strftime('%Y-%m-%dT%H:%M:%SZ'),
                "targetProfileName": profile_name,
                "scaleType": choice(['DOWN', 'UP']) 
            }
        }
        body
        try:
            api_response = api.create_namespaced_custom_object("hepapi.com", 'v1', namespace, "hpaprescalers", body)
            api_response
            print('Created HpaPrescaler Object.')
        except ApiException as e:
            print(f'Failed to create HpaPrescaler obj.: {str(e)}')
        
    
if __name__ == '__main__':
    name_prefix='destt3-'
    app_name_list=['nginx-ingress',]
    profile_name_list=['p1','p2','p3']
    namespace='default'
    obj_count=5
    time_between_mins=0.5
    generate_hpaprescaler_objects(
        name_prefix,
        app_name_list,
        profile_name_list,
        namespace,
        obj_count,
        time_between_mins,
    )
    
    
    