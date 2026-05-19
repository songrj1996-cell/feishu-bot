import asyncio

import httpx

from feishu import FEISHU_BASE, get_tenant_access_token


async def _upload_md_file(filename: str, content: bytes) -> str:
    access = await get_tenant_access_token()
    files = {
        "file": (filename, content, "text/markdown"),
    }
    data = {
        "file_name": filename,
        "parent_type": "explorer",
        "parent_node": "",
        "size": str(len(content)),
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{FEISHU_BASE}/drive/v1/files/upload_all",
            headers={"Authorization": f"Bearer {access}"},
            files=files,
            data=data,
        )
        result = resp.json()
        if result.get("code") != 0:
            raise RuntimeError(f"上传 md 文件失败: {result}")
        return result["data"]["file_token"]


async def _create_import_task(file_token: str, file_name: str) -> str:
    access = await get_tenant_access_token()
    body = {
        "file_extension": "md",
        "file_token": file_token,
        "type": "docx",
        "file_name": file_name.removesuffix(".md"),
        "point": {"mount_type": 1, "mount_key": ""},
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{FEISHU_BASE}/drive/v1/import_tasks",
            headers={"Authorization": f"Bearer {access}"},
            json=body,
        )
        result = resp.json()
        if result.get("code") != 0:
            raise RuntimeError(f"创建导入任务失败: {result}")
        return result["data"]["ticket"]


async def _poll_import_task(ticket: str, timeout_seconds: int = 120) -> dict:
    access = await get_tenant_access_token()
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            resp = await client.get(
                f"{FEISHU_BASE}/drive/v1/import_tasks/{ticket}",
                headers={"Authorization": f"Bearer {access}"},
            )
            result = resp.json()
            if result.get("code") != 0:
                raise RuntimeError(f"查询导入任务失败: {result}")

            task = result["data"]["result"]
            job_status = task.get("job_status")
            # 0 = success；1 = init；2 = processing；其他 = error
            if job_status in (1, 2):
                if asyncio.get_event_loop().time() > deadline:
                    raise RuntimeError("导入任务超时")
                await asyncio.sleep(1.5)
                continue
            if job_status == 0 and task.get("token"):
                return {
                    "token": task["token"],
                    "type": task.get("type", "docx"),
                    "url": task.get("url"),
                }
            raise RuntimeError(f"导入任务失败: status={job_status}, msg={task.get('job_error_msg')}")


async def _transfer_owner(doc_token: str, doc_type: str, user_open_id: str) -> None:
    access = await get_tenant_access_token()
    body = {"member_type": "openid", "member_id": user_open_id}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{FEISHU_BASE}/drive/v1/permissions/{doc_token}/members/transfer_owner",
            headers={
                "Authorization": f"Bearer {access}",
                "Content-Type": "application/json; charset=utf-8",
            },
            params={"type": doc_type},
            json=body,
        )
        result = resp.json()
        if result.get("code") != 0:
            raise RuntimeError(f"转移文档所有权失败: {result}")


async def create_doc_from_markdown(
    title: str, content: str, owner_open_id: str | None = None
) -> str:
    """生成飞书 docx 文档，返回文档 URL。

    若提供 owner_open_id，文档创建后会把所有权转给该用户（飞书会自动把
    文件移到该用户的"我的空间"下）。机器人默认保留 full_access 权限。
    """
    md_bytes = content.encode("utf-8")
    file_name = f"{title}.md"

    file_token = await _upload_md_file(file_name, md_bytes)
    ticket = await _create_import_task(file_token, file_name)
    result = await _poll_import_task(ticket, timeout_seconds=180)

    doc_token = result["token"]
    doc_type = result.get("type") or "docx"
    doc_url = result.get("url") or f"https://feishu.cn/{doc_type}/{doc_token}"

    if owner_open_id:
        await _transfer_owner(doc_token, doc_type, owner_open_id)

    return doc_url
