
#### This code searches for unhealthy pods and delete them automatically based on threshold set on the cluster 

This code also have capability to delete Unhealthy deploymenets for image/error pull errors. It can also allow to manually kill pods

##### pip3 install -r requirements.txt

##### python3 k8-monitoring.py (or) python3 k8-monitoring.py --threshold 5

Open Console on localhost:5000 and set number of clusters and load kubeconfig file for monitoring

Also, Console CLI can display messages for debug



#### ScreenShot


<img width="1688" alt="image" src="https://user-images.githubusercontent.com/48805484/229671037-4e40db12-8fc3-4e74-875d-a4ed3385c804.png">
