import asyncio
import time
import httpx
import numpy
import logging
import argparse
import json
import random
from openai import AsyncOpenAI

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


# Avoid client side connection error: https://github.com/encode/httpx/discussions/3084
http_client = httpx.AsyncClient(
    limits=httpx.Limits(
        max_connections=10000, max_keepalive_connections=10000, keepalive_expiry=30
    )
)

SAMPLE_PROMPTS = [
    "Explain how blockchain technology works, and provide a real-world example of its application outside of cryptocurrency.",
    "Compare and contrast the philosophies of Nietzsche and Kant, including their views on morality and human nature.",
    "Imagine you're a travel blogger. Write a detailed post describing a week-long adventure through rural Japan.",
    "Write a fictional letter from Albert Einstein to a modern-day physicist, discussing the current state of quantum mechanics.",
    "Provide a comprehensive explanation of how transformers work in machine learning, including attention mechanisms and positional encoding.",
    "Draft a business proposal for launching a new AI-powered productivity app, including target audience, key features, and a monetization strategy.",
    "Simulate a panel discussion between Elon Musk, Marie Curie, and Sun Tzu on the topic of 'Leadership in Times of Crisis'.",
    "Describe the process of photosynthesis in depth, and explain its importance in the global carbon cycle.",
    "Analyze the impact of social media on political polarization, citing relevant studies or historical examples.",
    "Write a short science fiction story where humans discover a parallel universe that operates under different physical laws.",
    "Explain the role of the Federal Reserve in the U.S. economy and how it manages inflation and unemployment.",
    "Describe the architecture of a modern web application, from frontend to backend, including databases, APIs, and deployment.",
    "Write an essay discussing whether artificial general intelligence (AGI) poses an existential threat to humanity.",
    "Summarize the key events and consequences of the Cuban Missile Crisis, and reflect on lessons for modern diplomacy.",
    "Create a guide for beginners on how to train a custom LLM using open-source tools and publicly available datasets.",
]


async def process_stream(stream):
    first_token_time = None
    total_tokens = 0
    async for chunk in stream:
        if first_token_time is None:
            first_token_time = time.time()
        if chunk.choices[0].delta.content:
            total_tokens += 1
        if chunk.choices[0].finish_reason is not None:
            break
    return first_token_time, total_tokens


async def make_request(
    client: AsyncOpenAI, model, max_completion_tokens, request_timeout
):
    start_time = time.time()
    content = random.choice(SAMPLE_PROMPTS)

    try:
        stream = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": content}],
            max_completion_tokens=max_completion_tokens,
            stream=True,
        )
        first_token_time, total_tokens = await asyncio.wait_for(
            process_stream(stream), timeout=request_timeout
        )

        end_time = time.time()
        elapsed_time = end_time - start_time
        ttft = first_token_time - start_time if first_token_time else None
        tokens_per_second = total_tokens / elapsed_time if elapsed_time > 0 else 0
        return total_tokens, elapsed_time, tokens_per_second, ttft

    except asyncio.TimeoutError:
        logging.warning(f"Request timed out after {request_timeout} seconds")
        return None
    except Exception as e:
        logging.error(f"Error during request: {str(e)}")
        return None


async def worker(
    client,
    model,
    semaphore,
    queue,
    results,
    max_completion_tokens,
    request_timeout,
):
    while True:
        async with semaphore:
            task_id = await queue.get()
            if task_id is None:
                queue.task_done()
                break
            logging.info(f"Starting request {task_id}")
            result = await make_request(
                client, model, max_completion_tokens, request_timeout
            )
            if result:
                results.append(result)
            else:
                logging.warning(f"Request {task_id} failed")
            queue.task_done()
            logging.info(f"Finished request {task_id}")


def calculate_percentile(values, percentile, reverse=False):
    if not values:
        return None
    if reverse:
        return numpy.percentile(values, 100 - percentile)
    return numpy.percentile(values, percentile)


async def preflight_check(client, model) -> bool:
    result = await make_request(client, model, 16, 5)
    return result is not None


async def main(
    model,
    num_requests,
    concurrency,
    request_timeout,
    max_completion_tokens,
    server_url,
    api_key,
):
    client = AsyncOpenAI(
        base_url=f"{server_url}/v1", api_key=api_key, http_client=http_client
    )

    if not await preflight_check(client, model):
        logging.error(
            "Preflight check failed. Please check configuration and the service status."
        )
        return

    semaphore = asyncio.Semaphore(concurrency)
    queue = asyncio.Queue()
    results = []

    # Add tasks to the queue
    for i in range(num_requests):
        await queue.put(i)

    # Add sentinel values to stop workers
    for _ in range(concurrency):
        await queue.put(None)

    # Create worker tasks
    workers = [
        asyncio.create_task(
            worker(
                client,
                model,
                semaphore,
                queue,
                results,
                max_completion_tokens,
                request_timeout,
            )
        )
        for _ in range(concurrency)
    ]

    start_time = time.time()

    # Wait for all tasks to complete
    await queue.join()
    await asyncio.gather(*workers)

    end_time = time.time()

    # Calculate metrics
    total_elapsed_time = end_time - start_time
    total_tokens = sum(tokens for tokens, _, _, _ in results if tokens is not None)
    latencies = [
        elapsed_time for _, elapsed_time, _, _ in results if elapsed_time is not None
    ]
    tokens_per_second_list = [tps for _, _, tps, _ in results if tps is not None]
    ttft_list = [ttft for _, _, _, ttft in results if ttft is not None]

    successful_requests = len(results)
    requests_per_second = (
        successful_requests / total_elapsed_time if total_elapsed_time > 0 else 0
    )
    avg_latency = sum(latencies) / len(latencies) if latencies else 0
    avg_tokens_per_second = (
        sum(tokens_per_second_list) / len(tokens_per_second_list)
        if tokens_per_second_list
        else 0
    )
    avg_ttft = sum(ttft_list) / len(ttft_list) if ttft_list else 0

    # Calculate percentiles
    percentiles = [50, 95, 99]
    latency_percentiles = [calculate_percentile(latencies, p) for p in percentiles]
    tps_percentiles = [
        calculate_percentile(tokens_per_second_list, p, reverse=True)
        for p in percentiles
    ]
    ttft_percentiles = [calculate_percentile(ttft_list, p) for p in percentiles]

    return {
        "model": model,
        "total_requests": num_requests,
        "successful_requests": successful_requests,
        "concurrency": concurrency,
        "request_timeout": request_timeout,
        "max_completion_tokens": max_completion_tokens,
        "total_time": total_elapsed_time,
        "requests_per_second": requests_per_second,
        "total_completion_tokens": total_tokens,
        "latency": {
            "average": avg_latency,
            "p50": latency_percentiles[0],
            "p95": latency_percentiles[1],
            "p99": latency_percentiles[2],
        },
        "tokens_per_second": {
            "average": avg_tokens_per_second,
            "p50": tps_percentiles[0],
            "p95": tps_percentiles[1],
            "p99": tps_percentiles[2],
        },
        "time_to_first_token": {
            "average": avg_ttft,
            "p50": ttft_percentiles[0],
            "p95": ttft_percentiles[1],
            "p99": ttft_percentiles[2],
        },
    }


def output_results(results, result_file=None):
    if result_file:
        with open(result_file, "w") as f:
            json.dump(results, f, indent=2)
        logging.info(f"Results saved to {result_file}")
    else:
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark Chat Completions API")
    parser.add_argument(
        "-m", "--model", type=str, required=True, help="Name of the model"
    )
    parser.add_argument(
        "-n",
        "--num-requests",
        type=int,
        default=100,
        help="Number of requests to make (default: 100)",
    )
    parser.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=10,
        help="Number of concurrent requests (default: 10)",
    )
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=300,
        help="Timeout for each request in seconds (default: 300)",
    )
    parser.add_argument(
        "--max-completion-tokens",
        type=int,
        default=128,
        help="Maximum number of tokens in the completion (default: 128)",
    )
    parser.add_argument(
        "--server-url",
        type=str,
        default="http://127.0.0.1",
        help="URL of the GPUStack server",
    )
    parser.add_argument("--api-key", type=str, default="fake", help="GPUStack API key")
    parser.add_argument(
        "--result-file",
        type=str,
        help="Result file path to save benchmark json results",
    )
    args = parser.parse_args()

    results = asyncio.run(
        main(
            args.model,
            args.num_requests,
            args.concurrency,
            args.request_timeout,
            args.max_completion_tokens,
            args.server_url,
            args.api_key,
        )
    )
    output_results(results, args.result_file)
