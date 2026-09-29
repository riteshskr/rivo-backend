import javafx.fxml.FXML;
import javafx.scene.control.*;
import javafx.scene.web.WebView;
import org.apache.http.client.methods.*;
import org.apache.http.entity.StringEntity;
import org.apache.http.impl.client.CloseableHttpClient;
import org.apache.http.impl.client.HttpClients;
import org.apache.http.util.EntityUtils;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

public class MainController {
    @FXML private TextField addressField;
    @FXML private WebView mapView;
    @FXML private Label distanceLabel, fareLabel;
    @FXML private TextField driverPhone;
    @FXML private PasswordField driverPassword;
    @FXML private VBox driverPanel;
    @FXML private Label driverName;

    private String authToken;  // JWT टोकन स्टोर करें
    private final String BASE_URL = "http://localhost:5000/api";  // Python बैकएंड URL

    @FXML
    public void initialize() {
        // Leaflet map को WebView में लोड करें (HTML फाइल से)
        mapView.getEngine().load(getClass().getResource("/map.html").toExternalForm());
    }

    // यूजर बुकिंग हैंडलर
    @FXML
    private void handleBook() {
        // मैप से लोकेशन लेकर बैकएंड पर POST करें
        // यहाँ कोड लिखें
    }

    // ड्राइवर लॉगिन
    @FXML
    private void handleDriverLogin() {
        String phone = driverPhone.getText();
        String pass = driverPassword.getText();

        try (CloseableHttpClient client = HttpClients.createDefault()) {
            HttpPost post = new HttpPost(BASE_URL + "/driver/login");
            post.setHeader("Content-Type", "application/json");
            
            String json = String.format("{\"phone\":\"%s\",\"password\":\"%s\"}", phone, pass);
            post.setEntity(new StringEntity(json));

            try (CloseableHttpResponse response = client.execute(post)) {
                String respJson = EntityUtils.toString(response.getEntity());
                ObjectMapper mapper = new ObjectMapper();
                JsonNode root = mapper.readTree(respJson);
                if (response.getStatusLine().getStatusCode() == 200) {
                    JsonNode driver = root.get("driver");
                    driverName.setText(driver.get("name").asText());
                    driverPanel.setVisible(true);
                    showAlert("सफलता", "ड्राइवर लॉगिन सफल");
                } else {
                    showAlert("त्रुटि", root.get("error").asText());
                }
            }
        } catch (Exception e) {
            e.printStackTrace();
        }
    }

    @FXML
    private void handleDriverLogout() {
        driverPanel.setVisible(false);
        driverName.setText("");
    }

    private void showAlert(String title, String message) {
        Alert alert = new Alert(Alert.AlertType.INFORMATION);
        alert.setTitle(title);
        alert.setContentText(message);
        alert.showAndWait();
    }
}