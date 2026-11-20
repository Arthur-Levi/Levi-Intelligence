import httpx

from app.integrations.prospecting import DiscoveredCompany, DiscoveryRequest


class ApifyClient:
    def __init__(self, token: str, actor_id: str) -> None:
        self.token = token
        self.actor_id = actor_id

    async def discover(self, request: DiscoveryRequest) -> list[DiscoveredCompany]:
        timeout = httpx.Timeout(180.0, connect=10.0)
        async with httpx.AsyncClient(
            timeout=timeout, headers={"Authorization": f"Bearer {self.token}"}
        ) as client:
            start = await client.post(
                f"https://api.apify.com/v2/acts/{self.actor_id}/runs",
                json={"searchStringsArray": [request.query], **request.filters},
            )
            start.raise_for_status()
            run_data = start.json().get("data", {})
            dataset_id = run_data.get("defaultDatasetId")
            run_id = run_data.get("id")
            if not dataset_id:
                raise RuntimeError(f"Apify did not return a dataset for run {run_id or 'unknown'}")

            # Apify run endpoint returns after actor starts. Poll a bounded period for completion.
            status_url = f"https://api.apify.com/v2/actor-runs/{run_id}"
            for _ in range(60):
                status_response = await client.get(status_url)
                status_response.raise_for_status()
                actor_status = status_response.json().get("data", {}).get("status")
                if actor_status in {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}:
                    break
                import asyncio

                await asyncio.sleep(2)
            else:
                raise TimeoutError("Apify actor did not finish within the configured wait window")
            if actor_status != "SUCCEEDED":
                raise RuntimeError(f"Apify actor finished with status {actor_status}")

            items_response = await client.get(
                f"https://api.apify.com/v2/datasets/{dataset_id}/items",
                params={"clean": "true", "format": "json", "limit": 1000},
            )
            items_response.raise_for_status()
            items = items_response.json()

        results: list[DiscoveredCompany] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("title") or item.get("businessName")
            if not name:
                continue
            website = item.get("website") or item.get("websiteUrl") or item.get("url")
            results.append(
                DiscoveredCompany(
                    name=str(name)[:200],
                    website_url=str(website)[:500] if website else None,
                    phone=item.get("phone") or item.get("phoneNumber"),
                    email=item.get("email"),
                    address=item.get("address")
                    if isinstance(item.get("address"), dict)
                    else {"formatted": item.get("address")}
                    if item.get("address")
                    else {},
                    industry=item.get("category") or item.get("industry"),
                    source_url=item.get("url") or item.get("placeUrl") or website,
                    external_id=str(item.get("placeId") or item.get("id") or "") or None,
                    raw=item,
                )
            )
        return results
