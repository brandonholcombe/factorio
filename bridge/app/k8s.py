"""Tiny in-cluster Kubernetes API client (scale + pod watch only)."""
import asyncio
import ssl

import aiohttp

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


class K8s:
    def __init__(self, namespace: str):
        self.namespace = namespace
        with open(f"{SA_DIR}/token") as f:
            self.token = f.read().strip()
        self.base = "https://kubernetes.default.svc"
        self.ssl = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")

    def _headers(self, content_type: str | None = None) -> dict:
        h = {"Authorization": f"Bearer {self.token}"}
        if content_type:
            h["Content-Type"] = content_type
        return h

    async def scale(self, deployment: str, replicas: int) -> None:
        url = (f"{self.base}/apis/apps/v1/namespaces/{self.namespace}"
               f"/deployments/{deployment}/scale")
        async with aiohttp.ClientSession() as s:
            async with s.patch(url, json={"spec": {"replicas": replicas}},
                               headers=self._headers("application/merge-patch+json"),
                               ssl=self.ssl) as r:
                r.raise_for_status()

    async def rollout_restart(self, deployment: str) -> None:
        import json as _json
        import time
        url = (f"{self.base}/apis/apps/v1/namespaces/{self.namespace}"
               f"/deployments/{deployment}")
        patch = {"spec": {"template": {"metadata": {"annotations": {
            "kubectl.kubernetes.io/restartedAt":
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}}}}}
        async with aiohttp.ClientSession() as s:
            async with s.patch(url, data=_json.dumps(patch),
                               headers=self._headers("application/strategic-merge-patch+json"),
                               ssl=self.ssl) as r:
                r.raise_for_status()

    async def pods(self, selector: str) -> list[dict]:
        url = (f"{self.base}/api/v1/namespaces/{self.namespace}/pods"
               f"?labelSelector={selector}")
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=self._headers(), ssl=self.ssl) as r:
                r.raise_for_status()
                return (await r.json()).get("items", [])

    async def wait_gone(self, selector: str, timeout_s: int = 300) -> None:
        for _ in range(timeout_s // 5):
            if not await self.pods(selector):
                return
            await asyncio.sleep(5)
        raise TimeoutError(f"pods {selector} still present after {timeout_s}s")

    async def wait_ready(self, selector: str, timeout_s: int = 420) -> None:
        for _ in range(timeout_s // 5):
            for pod in await self.pods(selector):
                for cond in pod.get("status", {}).get("conditions", []):
                    if cond["type"] == "Ready" and cond["status"] == "True":
                        return
            await asyncio.sleep(5)
        raise TimeoutError(f"no ready pod for {selector} after {timeout_s}s")
