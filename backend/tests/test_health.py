import unittest

from fastapi.testclient import TestClient

from backend.app.main import app


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.client = self.enterContext(TestClient(app))

    def test_health_returns_http_200(self):
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_health_reports_ok_status(self):
        self.assertEqual(self.client.get("/health").json()["status"], "ok")

    def test_health_reports_backend_service(self):
        self.assertEqual(self.client.get("/health").json(), {
            "status": "ok", "service": "enterprise-hrms-backend",
        })

    def test_unknown_endpoint_returns_http_404(self):
        self.assertEqual(self.client.get("/unknown").status_code, 404)


if __name__ == "__main__":
    unittest.main()
