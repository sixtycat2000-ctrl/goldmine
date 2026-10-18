# Kubernetes Notes

The scheduler evicts a pod when a node is under pressure and the pod has no toleration.

Taints and tolerations work together: the kubelet taints a node, pods without a
matching toleration are evicted.
