import asyncio
import redis.asyncio as redis

# Substitua pela URL da sua Cloud Redis (ex: redis://:senha@host:porta)
REDIS_URL = "redis://default:A6HXlIKJ2O1SnTazWbCmlIkXOaesxVdH@rod-toys-paramount-66968.db.redis.io:19251"


async def purge_unexpired_idempotency_keys():
    client = redis.from_url(REDIS_URL)
    print("Conectado ao Redis. Iniciando varredura de chaves de idempotência...")
    count = 0
    # SCAN iterativo para não travar o Redis em produção
    async for key in client.scan_iter(match="knt:eventids:*", count=1000):
        ttl = await client.ttl(key)
        # Se a chave for antiga e não tiver TTL (-1), aplica expiração ou apaga
        if ttl == -1:
            await client.expire(key, 86400)  # Define 24h para expirar gradualmente
            # Ou use: await client.unlink(key)  # Para apagar IMEDIATAMENTE da memória
            count += 1
            if count % 10000 == 0:
                print(f"Processadas {count} chaves...")
    print(f"✅ Concluído! Expiração aplicada em {count} chaves antigas acumuladas.")
    await client.close()


if __name__ == "__main__":
    asyncio.run(purge_unexpired_idempotency_keys())
