
S3_BASE_URL = "https://uk1s3.embassy.ebi.ac.uk/model-repository/"
TRITON_IMAGE = "docker://nvcr.io/nvidia/tritonserver:23.03-py3"
S3_IMAGE = "docker://minio/minio:RELEASE.2022-09-01T23-53-36Z.fips"

IMJOY_APP_TEMPLATE = """
{app_code}

async def main():
    services = []
    server = api
    _register_service = server.register_service
    async def patched_register_service(*args, **kwargs):
        svc_info = await _register_service(*args, **kwargs)
        services.append(svc_info)
        return svc_info
    server.register_service = patched_register_service
    server.registerService = patched_register_service

    try:
        client_id = server.rpc.get_client_info()['id']
        await hypha_startup(server)
        report_service = await server.get_service("hypha-launcher")
        await report_service.report_services(client_id, services)
    except Exception as e:
        print("Failed to start hypha server:", e)
        await report_service.report_error(client_id, str(e))

import asyncio
loop = asyncio.get_event_loop()
loop.create_task(main)
loop.run_forever()
"""
