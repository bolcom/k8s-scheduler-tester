#!/bin/sh

CLUSTER_NAME=example
NAMESPACE=k8s-scheduler-tester
CONTEXT=kind-$CLUSTER_NAME


echo "Waiting for running pod ..."
while :; do
  pod=$(kubectl --context $CONTEXT get -n $NAMESPACE pod -l name=k8s-scheduler-tester -ojsonpath='{.items[0].metadata.name}' --field-selector status.phase=Running)
  [ -n "$pod" ] && break
  sleep 0.2
done
echo "Found!"

kubectl --context $CONTEXT -n $NAMESPACE exec -n $NAMESPACE $pod -- python -c 'import requests;print(requests.get("http://localhost:9999").text)'
