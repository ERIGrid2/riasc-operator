import kopf

import riasc_operator.project  # noqa: F401
import riasc_operator.time_sync  # noqa: F401


def main():
    kopf.configure(
        verbose=True
    )

    kopf.run(
        clusterwide=True,
        liveness_endpoint='http://0.0.0.0:8080'
    )
